#!/usr/bin/env python3
"""
Demo 工具：基于 reconcile-ai 自动解决 GitHub PR 的代码冲突并提交
使用方法: python resolve_pr_conflicts.py <PR_URL> 或 python resolve_pr_conflicts.py <owner/repo> <PR_NUMBER>

环境变量:
  GITHUB_TOKEN - GitHub API token (必需)
  OPENAI_API_KEY - OpenAI API key (必需)
  OPENAI_API_BASE - 自定义 OpenAI API 地址 (可选，用于国内代理)
    例如: https://api.jiekou.ai/openai
  RECONCILE_MODEL - 使用的模型 (可选，默认: gpt-4o)
"""

import os
import sys
import re
import subprocess
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("❌ 需要安装 requests: pip install requests")
    sys.exit(1)

try:
    from reconcile import (
        detect_conflicts,
        parse_conflicts,
        resolve_conflict_sections_batch,
        resolve_conflict_section_single,
        apply_resolutions,
        load_config,
        setup_logging
    )
    import reconcile
    from git import Repo
except ImportError:
    print("❌ 需要安装 reconcile-ai: pip install reconcile-ai")
    print("   还需要安装 GitPython: pip install GitPython")
    sys.exit(1)


# 修复 reconcile-ai 对 JieKou.AI 的处理
_original_get_client = reconcile._get_openai_client

def _patched_get_client(config=None):
    """修改后的函数，对 JieKou.AI 不添加 /v1"""
    import os
    from openai import OpenAI
    
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "❌ OpenAI API key not found!\n\n"
            "To use AI-powered conflict resolution, you need to:\n"
            "1. Get an API key from https://platform.openai.com/api-keys\n"
            "2. Set it as an environment variable:\n"
            "   export OPENAI_API_KEY='your-api-key-here'\n\n"
            "Alternatively, you can use the --dry-run flag to see conflicts without AI resolution."
        )
    
    if config and config.get('api_base_url'):
        api_base_url = config['api_base_url']
    else:
        api_base_url = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    
    # 对于 JieKou.AI，不添加 /v1
    if 'jiekou.ai' in api_base_url:
        pass
    else:
        if not api_base_url.endswith('/v1'):
            if api_base_url.endswith('/'):
                api_base_url = api_base_url + 'v1'
            else:
                api_base_url = api_base_url + '/v1'
    
    return OpenAI(api_key=api_key, base_url=api_base_url)

reconcile._get_openai_client = _patched_get_client


def parse_conflicts_smart(content):
    """
    智能解析冲突块，正确处理嵌套冲突
    只提取最外层的完整冲突块，嵌套冲突的内容包含在最外层冲突中
    """
    lines = content.split('\n')
    conflicts = []
    depth = 0  # 跟踪嵌套深度
    current_conflict = None
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        if stripped.startswith('<<<<<<<'):
            if depth == 0:
                # 开始一个新的最外层冲突
                current_conflict = {
                    'start': i,
                    'lines': [line],
                    'has_separator': False
                }
            else:
                # 嵌套冲突，只记录到当前冲突中，不单独处理
                if current_conflict:
                    current_conflict['lines'].append(line)
            depth += 1
        
        elif stripped.startswith('======='):
            if current_conflict:
                current_conflict['lines'].append(line)
                # 只有最外层的分隔符才标记（depth == 1 表示刚进入最外层冲突）
                if depth == 1:
                    current_conflict['has_separator'] = True
        
        elif stripped.startswith('>>>>>>>'):
            if current_conflict:
                current_conflict['lines'].append(line)
            depth -= 1
            
            # 如果回到最外层（depth == 0），说明找到了完整的冲突块
            if depth == 0 and current_conflict:
                # 验证冲突块是否完整（必须有分隔符）
                if current_conflict.get('has_separator'):
                    conflict_text = '\n'.join(current_conflict['lines'])
                    conflicts.append(conflict_text)
                current_conflict = None
        
        else:
            # 普通内容行
            if current_conflict:
                current_conflict['lines'].append(line)
    
    return conflicts


def clean_ai_response(resolved_text):
    """
    清理 AI 返回的解决方案，移除 markdown 代码块、解释文字等
    如果包含错误信息或无效内容，返回 None 表示应该回退
    """
    if not resolved_text:
        return None
    
    # 检查是否包含错误信息或无效内容
    error_indicators = [
        "It seems",
        "does not contain",
        "Please provide",
        "Please share",
        "I cannot",
        "I'm unable",
        "I don't have",
        "cannot resolve",
        "unable to resolve"
    ]
    
    for indicator in error_indicators:
        if indicator.lower() in resolved_text.lower():
            return None
    
    # 移除 markdown 代码块标记
    code_block_pattern = r'```(?:\w+)?\s*\n(.*?)\n```'
    matches = re.findall(code_block_pattern, resolved_text, re.DOTALL)
    if matches:
        resolved_text = matches[-1]
    else:
        if '```' in resolved_text:
            parts = resolved_text.split('```')
            if len(parts) >= 2:
                potential_code = parts[1]
                potential_code = re.sub(r'^\w+\s*\n', '', potential_code)
                if potential_code.strip():
                    resolved_text = potential_code
    
    # 移除常见的解释性文字
    explanation_patterns = [
        r'^Explanation:.*?\n',
        r'^Here is.*?:\n',
        r'^The resolved code.*?:\n',
        r'^RESOLUTION\s+\d+:.*?\n',
        r'^---.*?\n',
        r'^\*\*RESOLUTION.*?\*\*.*?\n',
    ]
    
    for pattern in explanation_patterns:
        resolved_text = re.sub(pattern, '', resolved_text, flags=re.MULTILINE | re.IGNORECASE)
    
    # 移除行首的 markdown 格式标记
    lines = resolved_text.split('\n')
    cleaned_lines = []
    skip_until_code = False
    
    for line in lines:
        if line.strip().startswith('**') and line.strip().endswith('**'):
            continue
        if line.strip() == '---':
            continue
        if re.match(r'^RESOLUTION\s+\d+:', line, re.IGNORECASE):
            skip_until_code = True
            continue
        if skip_until_code and (line.strip().startswith('```') or not line.strip()):
            if line.strip().startswith('```'):
                skip_until_code = False
            continue
        
        cleaned_lines.append(line)
    
    resolved_text = '\n'.join(cleaned_lines).strip()
    
    # 确保没有冲突标记
    if '<<<<<<< HEAD' in resolved_text or '=======' in resolved_text or '>>>>>>>' in resolved_text:
        code_blocks = re.findall(r'```(?:\w+)?\s*\n(.*?)\n```', resolved_text, re.DOTALL)
        if code_blocks:
            resolved_text = code_blocks[-1].strip()
        # 移除包含冲突标记的行
        lines = resolved_text.split('\n')
        cleaned_lines = []
        for line in lines:
            if not (line.strip().startswith('<<<<<<<') or 
                    line.strip().startswith('=======') or 
                    line.strip().startswith('>>>>>>>')):
                cleaned_lines.append(line)
        resolved_text = '\n'.join(cleaned_lines)
    
    if not resolved_text.strip():
        return None
    
    return resolved_text


def validate_resolution(original_section, resolved_text):
    """
    验证解决方案是否合理
    只检查：不能包含冲突标记和错误信息
    """
    if not resolved_text:
        return False
    
    # 检查冲突标记
    if check_conflict_markers(resolved_text):
        return False
    
    # 检查错误信息
    error_indicators = [
        "It seems",
        "does not contain",
        "Please provide",
        "Please share",
        "I cannot",
        "I'm unable",
        "I don't have",
        "cannot resolve",
        "unable to resolve"
    ]
    
    for indicator in error_indicators:
        if indicator.lower() in resolved_text.lower():
            return False
    
    return True


def check_conflict_markers(content):
    """检查内容中是否包含冲突标记"""
    has_start = bool(re.search(r'^<<<<<<<', content, re.MULTILINE))
    has_separator = bool(re.search(r'^=======', content, re.MULTILINE))
    has_end = bool(re.search(r'^>>>>>>>', content, re.MULTILINE))
    
    return has_start or has_separator or has_end


def is_complete_conflict(section):
    """
    检查冲突块是否完整
    完整的冲突块应该包含：<<<<<<< HEAD、======= 和 >>>>>>>
    """
    has_start = bool(re.search(r'^<<<<<<<', section, re.MULTILINE))
    has_separator = bool(re.search(r'^=======', section, re.MULTILINE))
    has_end = bool(re.search(r'^>>>>>>>', section, re.MULTILINE))
    
    return has_start and has_separator and has_end


def extract_conflict_core(conflict_section):
    """
    提取冲突的核心内容（去掉标记行，只保留实际代码）
    用于匹配，忽略分支名等差异
    """
    lines = conflict_section.split('\n')
    core_lines = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('<<<<<<<') or stripped.startswith('=======') or stripped.startswith('>>>>>>>'):
            continue
        core_lines.append(line)
    
    return '\n'.join(core_lines).strip()


def remove_duplicate_lines(content, context_lines=3):
    """
    移除连续的重复代码行
    避免误删合法的重复结构（如花括号）
    改进：对花括号更保守，只删除明显异常的重复
    """
    lines = content.split('\n')
    cleaned_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        cleaned_lines.append(line)
        
        # 对于花括号，非常保守：只删除连续超过5个的重复
        if line.strip() in ['{', '}']:
            j = i + 1
            duplicate_count = 0
            while j < len(lines) and lines[j].strip() == line.strip():
                duplicate_count += 1
                j += 1
            
            # 只有连续超过5个花括号才删除多余的
            if duplicate_count > 5:
                # 保留第一个，跳过多余的
                i = j
            else:
                i += 1
            continue
        
        # 对于其他行，检查重复
        j = i + 1
        duplicate_count = 0
        
        while j < len(lines) and j < i + context_lines + 1:
            current_line = lines[j]
            
            if current_line.strip() == line.strip() and line.strip():
                # 跳过注释行
                if line.strip().startswith('//') or line.strip().startswith('#'):
                    break
                duplicate_count += 1
                j += 1
            else:
                break
        
        if duplicate_count > 0:
            # 跳过重复的行
            i = j
        else:
            i += 1
    
    return '\n'.join(cleaned_lines)


def detect_duplicate_code(content):
    """
    检测代码中的重复行
    返回重复行的位置和内容
    改进：忽略花括号的重复（这是正常的代码结构）
    """
    lines = content.split('\n')
    duplicates = []
    
    i = 0
    while i < len(lines) - 1:
        line_stripped = lines[i].strip()
        
        # 忽略花括号的重复（这是正常的代码结构）
        if line_stripped in ['{', '}']:
            i += 1
            continue
        
        # 忽略空行
        if not line_stripped:
            i += 1
            continue
        
        if line_stripped == lines[i + 1].strip():
            dup_start = i
            dup_line = line_stripped
            j = i + 1
            while j < len(lines) and lines[j].strip() == dup_line:
                j += 1
            # 只报告连续重复超过2次的情况
            if j - i > 2:
                duplicates.append({
                    'line': dup_start + 1,
                    'content': dup_line,
                    'count': j - i
                })
            i = j
        else:
            i += 1
    
    return duplicates


def format_conflict_preview(conflict_section, max_lines=5):
    """
    格式化冲突预览，显示冲突的关键内容
    """
    if not is_complete_conflict(conflict_section):
        return f"  ⚠️  不完整的冲突块（缺少标记）\n  {conflict_section[:100]}..."
    
    lines = conflict_section.split('\n')
    preview_lines = []
    in_head = False
    in_merge = False
    content_lines_shown = 0
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('<<<<<<<'):
            preview_lines.append('  <<<<<<< HEAD')
            in_head = True
            in_merge = False
            content_lines_shown = 0
        elif stripped.startswith('======='):
            preview_lines.append('  =======')
            in_head = False
            in_merge = True
            content_lines_shown = 0
        elif stripped.startswith('>>>>>>>'):
            preview_lines.append(f'  >>>>>>> {stripped[8:60]}')
            break
        elif in_head or in_merge:
            # 只显示前几行内容
            if content_lines_shown < max_lines:
                preview_lines.append(f'    {line[:60]}')
                content_lines_shown += 1
    
    return '\n'.join(preview_lines)


def parse_pr_url(pr_url):
    """解析 PR URL 或参数"""
    if pr_url.startswith("http"):
        url = urlparse(pr_url)
        if url.hostname != "github.com":
            raise ValueError("只支持 GitHub PR URL")
        
        path_parts = url.path.strip("/").split("/")
        if len(path_parts) < 4 or path_parts[2] != "pull":
            raise ValueError("无效的 PR URL 格式")
        
        owner = path_parts[0]
        repo = path_parts[1]
        pr_number = int(path_parts[3])
        return owner, repo, pr_number
    else:
        if len(sys.argv) < 3:
            raise ValueError("使用 PR 编号时需要提供 owner/repo 和 PR_NUMBER")
        repo_parts = pr_url.split("/")
        if len(repo_parts) != 2:
            raise ValueError("仓库格式错误，应为 owner/repo")
        pr_number = int(sys.argv[2])
        return repo_parts[0], repo_parts[1], pr_number


def get_pr_info(owner, repo, pr_number, token):
    """从 GitHub API 获取 PR 信息"""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {"Authorization": f"token {token}"}
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    pr_data = response.json()
    return {
        "head_ref": pr_data["head"]["ref"],
        "base_ref": pr_data["base"]["ref"],
        "title": pr_data.get("title", ""),
        "head_sha": pr_data["head"]["sha"],
        "mergeable": pr_data.get("mergeable"),
        "mergeable_state": pr_data.get("mergeable_state", ""),
        "merged": pr_data.get("merged", False)
    }


def run_git_cmd(repo_path, args, capture_stderr=True):
    """运行 Git 命令，返回 stdout 和 stderr"""
    result = subprocess.run(
        ["git"] + args,
        cwd=repo_path,
        capture_output=True,
        text=True
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def test_api_connection(api_key, api_base, model=None):
    """测试 API 连接"""
    try:
        from openai import OpenAI
        
        test_model = model or os.getenv("RECONCILE_MODEL") or "gpt-4o"
        
        if api_base:
            test_base = api_base
        else:
            test_base = "https://api.openai.com/v1"
        
        print(f"   测试模型: {test_model}")
        print(f"   API 地址: {test_base}")
        
        client = OpenAI(api_key=api_key, base_url=test_base)
        
        response = client.chat.completions.create(
            model=test_model,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )
        print(f"✅ API 连接成功，使用的模型: {test_model}")
        return True, None
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg or "MODEL_NOT_FOUND" in error_msg or "model not found" in error_msg.lower():
            return False, f"模型不支持: {test_model}. 错误: {error_msg}"
        return False, error_msg


def clone_repo_optimized(repo_url, repo_path, head_ref, base_ref, token):
    """优化的仓库克隆 - 使用浅克隆和单分支"""
    print("📥 克隆仓库（优化模式）...")
    
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    
    clone_cmd = [
        "git", "clone",
        "--depth", "1",
        "--single-branch",
        "--branch", head_ref,
        repo_url,
        str(repo_path)
    ]
    
    result = subprocess.run(
        clone_cmd,
        cwd=str(repo_path.parent),
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print(f"⚠️  无法直接克隆 {head_ref} 分支，尝试克隆 {base_ref}...")
        clone_cmd_base = [
            "git", "clone",
            "--depth", "1",
            "--single-branch",
            "--branch", base_ref,
            repo_url,
            str(repo_path)
        ]
        
        result = subprocess.run(
            clone_cmd_base,
            cwd=str(repo_path.parent),
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            print(f"❌ 克隆失败: {result.stderr}")
            return False
        
        print(f"🔀 切换到 PR 分支: {head_ref}")
        stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{head_ref}:{head_ref}", "--depth", "1"])
        if code != 0:
            stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{head_ref}:{head_ref}"])
            if code != 0:
                print(f"⚠️  获取 PR 分支失败: {stderr}")
                return False
        
        stdout, stderr, code = run_git_cmd(repo_path, ["checkout", head_ref])
        if code != 0:
            print(f"❌ 切换分支失败: {stderr}")
            return False
    
    print(f"📥 获取目标分支: {base_ref}")
    
    stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{base_ref}:origin/{base_ref}", "--depth", "10"])
    if code != 0:
        print(f"⚠️  浅克隆获取目标分支失败，尝试增加深度...")
        stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{base_ref}:origin/{base_ref}", "--depth", "50"])
        if code != 0:
            print(f"⚠️  增加深度后仍失败，尝试完整获取...")
            stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", base_ref])
            if code != 0:
                print(f"❌ 获取目标分支失败: {stderr}")
                return False
    
    return True


def apply_resolutions_safe(file_path, original_content, resolved_map):
    """
    安全地应用解决方案
    核心逻辑：
    1. 找到冲突标记（<<<<<<< HEAD ... ======= ... >>>>>>>）
    2. 用 AI 返回的解决方案替换冲突标记
    3. 彻底清理所有残留的冲突标记
    """
    updated = original_content
    replaced_count = 0
    
    # 清理和验证所有解决方案
    cleaned_resolved_map = {}
    for section, resolved in resolved_map.items():
        if resolved is None:
            print(f"   ⚠️  解决方案无效，跳过此冲突（保留原始冲突标记）")
            continue
            
        cleaned = clean_ai_response(resolved)
        
        if cleaned is None:
            print(f"   ⚠️  解决方案包含错误信息，跳过此冲突（保留原始冲突标记）")
            continue
        
        if not validate_resolution(section, cleaned):
            print(f"   ⚠️  解决方案验证失败，跳过此冲突（保留原始冲突标记）")
            continue
        
        if check_conflict_markers(cleaned):
            print(f"   ⚠️  警告: 清理后的解决方案仍包含冲突标记，尝试进一步清理...")
            lines = cleaned.split('\n')
            cleaned_lines = []
            for line in lines:
                if not (line.strip().startswith('<<<<<<<') or 
                        line.strip().startswith('=======') or 
                        line.strip().startswith('>>>>>>>')):
                    cleaned_lines.append(line)
            cleaned = '\n'.join(cleaned_lines)
            
            if check_conflict_markers(cleaned):
                print(f"   ⚠️  无法清理冲突标记，跳过此冲突（保留原始冲突标记）")
                continue
        
        cleaned_resolved_map[section] = cleaned
    
    if not cleaned_resolved_map:
        print(f"   ⚠️  所有解决方案都无效，保留原始冲突标记")
        return updated, 0
    
    # 使用智能解析找到所有完整的冲突块（包括嵌套的）
    all_conflicts = parse_conflicts_smart(updated)
    
    # 从后往前处理，避免位置偏移
    for conflict_text in reversed(all_conflicts):
        # 找到冲突在文件中的位置（使用精确匹配）
        conflict_start = updated.find(conflict_text)
        if conflict_start == -1:
            # 如果精确匹配失败，尝试使用正则表达式匹配（忽略空白差异）
            conflict_lines = conflict_text.split('\n')
            pattern = re.escape(conflict_lines[0])
            for line in conflict_lines[1:]:
                pattern += r'\s*\n\s*' + re.escape(line)
            match = re.search(pattern, updated, re.MULTILINE)
            if match:
                conflict_start = match.start()
                conflict_text = match.group(0)
            else:
                continue
        
        conflict_end = conflict_start + len(conflict_text)
        
        # 找到对应的解决方案
        resolved = None
        for original_section, cleaned_resolved in cleaned_resolved_map.items():
            # 提取核心内容用于匹配
            original_core = extract_conflict_core(original_section)
            current_core = extract_conflict_core(conflict_text)
            
            # 精确匹配核心内容
            if original_core.strip() == current_core.strip():
                resolved = cleaned_resolved
                break
        
        # 如果找不到精确匹配，尝试通过内容匹配
        if resolved is None:
            conflict_lines = conflict_text.split('\n')
            conflict_head = []
            conflict_merge = []
            in_head = True
            
            for line in conflict_lines:
                stripped = line.strip()
                if stripped.startswith('<<<<<<<'):
                    continue
                elif stripped.startswith('======='):
                    in_head = False
                    continue
                elif stripped.startswith('>>>>>>>'):
                    break
                elif in_head:
                    conflict_head.append(line.strip())
                else:
                    conflict_merge.append(line.strip())
            
            for original_section, cleaned_resolved in cleaned_resolved_map.items():
                original_lines = original_section.split('\n')
                orig_head = []
                orig_merge = []
                in_head = True
                
                for line in original_lines:
                    stripped = line.strip()
                    if stripped.startswith('<<<<<<<'):
                        continue
                    elif stripped.startswith('======='):
                        in_head = False
                        continue
                    elif stripped.startswith('>>>>>>>'):
                        break
                    elif in_head:
                        orig_head.append(line.strip())
                    else:
                        orig_merge.append(line.strip())
                
                # 比较前几行关键内容
                head_match = False
                if orig_head and conflict_head:
                    head_match = any(h.strip() in ' '.join(conflict_head[:10]) for h in orig_head[:5] if h.strip())
                
                merge_match = False
                if orig_merge and conflict_merge:
                    merge_match = any(m.strip() in ' '.join(conflict_merge[:10]) for m in orig_merge[:5] if m.strip())
                
                if head_match or merge_match:
                    resolved = cleaned_resolved
                    break
        
        if resolved:
            # 替换冲突标记
            updated = updated[:conflict_start] + resolved + updated[conflict_end:]
            replaced_count += 1
    
    # 彻底清理所有残留的冲突标记
    # 1. 清理孤立的冲突标记行
    lines = updated.split('\n')
    cleaned_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        
        if stripped.startswith('<<<<<<<') or stripped.startswith('=======') or stripped.startswith('>>>>>>>'):
            # 检查前后是否有完整的冲突标记结构
            has_start = False
            for j in range(max(0, i - 50), i):
                if lines[j].strip().startswith('<<<<<<<'):
                    has_start = True
                    break
            
            has_end = False
            for j in range(i + 1, min(len(lines), i + 50)):
                if lines[j].strip().startswith('>>>>>>>'):
                    has_end = True
                    break
            
            if not (has_start and has_end):
                print(f"   🔧 清理孤立的冲突标记: 第 {i+1} 行 ({stripped[:50]})")
                i += 1
                continue
        
        cleaned_lines.append(line)
        i += 1
    
    updated = '\n'.join(cleaned_lines)
    
    # 2. 使用正则表达式彻底清理所有残留的冲突标记
    max_cleanup_iterations = 5
    for _ in range(max_cleanup_iterations):
        before = updated
        updated = re.sub(r'^<<<<<<<[^\n]*\n', '', updated, flags=re.MULTILINE)
        updated = re.sub(r'^=======\s*\n', '', updated, flags=re.MULTILINE)
        updated = re.sub(r'^>>>>>>>[^\n]*\n', '', updated, flags=re.MULTILINE)
        if before == updated:
            break
    
    # 3. 最终验证：确保没有残留的冲突标记
    if check_conflict_markers(updated):
        lines = updated.split('\n')
        final_cleaned = []
        for line in lines:
            stripped = line.strip()
            if not (stripped.startswith('<<<<<<<') or 
                    stripped.startswith('=======') or 
                    stripped.startswith('>>>>>>>')):
                final_cleaned.append(line)
        updated = '\n'.join(final_cleaned)
    
    # 4. 移除重复的代码行（避免误删花括号）
    updated = remove_duplicate_lines(updated)
    
    # 5. 清理多余的空行
    updated = re.sub(r'\n{3,}', '\n\n', updated)
    
    # 写入文件
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(updated)
    
    return updated, replaced_count


def main():
    if len(sys.argv) < 2:
        print("使用方法:")
        print("  python resolve_pr_conflicts.py <PR_URL>")
        print("  例如: python resolve_pr_conflicts.py https://github.com/owner/repo/pull/123")
        print()
        print("或者:")
        print("  python resolve_pr_conflicts.py <owner/repo> <PR_NUMBER>")
        print("  例如: python resolve_pr_conflicts.py owner/repo 123")
        sys.exit(1)
    
    # 获取 GitHub Token
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ 错误: 需要设置 GITHUB_TOKEN 环境变量")
        sys.exit(1)
    
    # 检查 OpenAI API 配置
    api_key = os.getenv("OPENAI_API_KEY")
    api_base = os.getenv("OPENAI_API_BASE")
    
    if not api_key:
        print("❌ 错误: 需要设置 OPENAI_API_KEY 环境变量")
        sys.exit(1)
    
    # 获取模型
    model = os.getenv("RECONCILE_MODEL") or "gpt-4o"
    
    # 处理 API base URL
    if api_base:
        print(f"ℹ️  使用自定义 API 地址: {api_base}")
        os.environ['OPENAI_API_BASE'] = api_base
        print(f"   实际 API 地址: {api_base}")
    else:
        print("ℹ️  使用默认 OpenAI API 地址: https://api.openai.com/v1")
    
    # 测试 API 连接
    print("🧪 测试 API 连接...")
    api_ok, api_error = test_api_connection(api_key, api_base, model=model)
    if not api_ok:
        print(f"❌ API 连接失败: {api_error}")
        if "401" in api_error or "invalid_api_key" in api_error.lower():
            print("💡 提示: API key 无效，请检查:")
            print("   1. 是否使用 JieKou.AI 平台提供的 API key")
            print("   2. API key 是否已激活")
            print("   3. 账户是否有余额")
        sys.exit(1)
    print("✅ API 连接正常")
    
    # 解析 PR 信息
    try:
        owner, repo_name, pr_number = parse_pr_url(sys.argv[1])
    except Exception as e:
        print(f"❌ 解析 PR 信息失败: {e}")
        sys.exit(1)
    
    print(f"🔍 获取 PR 信息: {owner}/{repo_name}#{pr_number}")
    
    # 获取 PR 详情
    try:
        pr_info = get_pr_info(owner, repo_name, pr_number, token)
    except Exception as e:
        print(f"❌ 获取 PR 信息失败: {e}")
        sys.exit(1)
    
    print(f"📋 PR 信息:")
    print(f"  标题: {pr_info['title']}")
    print(f"  源分支: {pr_info['head_ref']}")
    print(f"  目标分支: {pr_info['base_ref']}")
    
    if pr_info['merged']:
        print("ℹ️  PR 已经合并，无需解决冲突")
        sys.exit(0)
    
    if pr_info['mergeable'] is False:
        print("⚠️  PR 标记为不可合并，可能已有冲突")
    elif pr_info['mergeable'] is True:
        print("ℹ️  PR 标记为可合并，可能没有冲突")
    else:
        print("ℹ️  PR 合并状态未知，继续尝试...")
    
    print(f"   合并状态: {pr_info['mergeable_state']}")
    
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir)
    print(f"📁 临时目录: {temp_dir}")
    
    try:
        # 优化的克隆方式
        repo_url = f"https://{token}@github.com/{owner}/{repo_name}.git"
        if not clone_repo_optimized(repo_url, repo_path, pr_info['head_ref'], pr_info['base_ref'], token):
            print("❌ 克隆仓库失败")
            sys.exit(1)
        
        # 配置 Git
        run_git_cmd(repo_path, ["config", "user.name", "reconcile-demo"])
        run_git_cmd(repo_path, ["config", "user.email", "reconcile-demo@example.com"])
        
        # 尝试合并
        print(f"🔀 尝试合并 {pr_info['base_ref']} 到 {pr_info['head_ref']}")
        merge_result = subprocess.run(
            ["git", "merge", f"origin/{pr_info['base_ref']}", "--allow-unrelated-histories"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        
        # 确保环境变量设置
        if api_base:
            os.environ['OPENAI_API_BASE'] = api_base
        
        # 使用 reconcile-ai 检测冲突（即使 merge 成功也要检查文件中的冲突标记）
        logger = setup_logging(verbose=True, json_logging=False)
        
        # 加载配置
        config = load_config(str(repo_path))
        
        # 在 config 中设置 api_base_url
        if api_base:
            config['api_base_url'] = api_base
        
        # 使用模型
        final_model = os.getenv("RECONCILE_MODEL") or config.get('model', model)
        max_batch_size = config.get('max_batch_size', 5)
        
        print(f"🤖 使用模型: {final_model}, 批量大小: {max_batch_size}")
        if api_base:
            print(f"🌐 API 地址: {api_base}")
        
        # 关键修改：无论 merge 是否成功，都检查文件中的冲突标记
        print("🔍 扫描文件中的冲突标记...")
        blobs = detect_conflicts(str(repo_path))
        
        if not blobs:
            if merge_result.returncode == 0:
                print("✅ PR 无冲突，无需解决")
                return
            else:
                stderr = merge_result.stderr
                stdout = merge_result.stdout
                if "CONFLICT" not in stderr and "冲突" not in stderr and "CONFLICT" not in stdout:
                    if "unrelated histories" in stderr.lower():
                        print(f"⚠️  检测到不相关历史，已使用 --allow-unrelated-histories")
                        print(f"❌ 合并失败: {stderr}")
                        sys.exit(1)
                    print(f"❌ 合并失败，但未检测到冲突标记")
                    print(f"   退出码: {merge_result.returncode}")
                    print(f"   错误信息: {stderr if stderr else '(无错误信息)'}")
                    sys.exit(1)
                else:
                    print("⚠️  Git 报告有冲突，但未在文件中找到冲突标记")
                    print("   可能冲突标记格式不标准，或已被部分解决")
                    sys.exit(1)
        
        # 关键改进：使用智能解析函数重新解析冲突
        conflicts = {}
        for path in blobs:
            full_path = repo_path / path if not Path(path).is_absolute() else Path(path)
            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                # 使用智能解析函数
                sections = parse_conflicts_smart(content)
                if sections:
                    conflicts[path] = sections
            except Exception as e:
                print(f"⚠️  解析文件 {path} 失败: {e}")
        
        git_repo = Repo(str(repo_path))
        
        total_conflicts = sum(len(sections) for sections in conflicts.values())
        print(f"📝 找到 {total_conflicts} 个冲突，分布在 {len(conflicts)} 个文件中")
        
        # 新增：显示每个文件的冲突详情
        print("\n📋 冲突详情:")
        for path, sections in conflicts.items():
            print(f"\n📄 {path}: {len(sections)} 个冲突")
            for i, section in enumerate(sections, 1):
                print(f"   冲突 {i}:")
                preview = format_conflict_preview(section, max_lines=3)
                print(preview)
        
        if total_conflicts == 0:
            if merge_result.returncode == 0:
                print("ℹ️  文件中可能有冲突标记，但解析后未找到有效冲突")
                print("   可能冲突标记格式不完整，跳过处理")
                return
            else:
                print("ℹ️  未找到冲突内容")
                sys.exit(0)
        
        if merge_result.returncode == 0:
            print("\n⚠️  检测到文件中有冲突标记，但 Git merge 已成功")
            print("   这可能是之前合并时遗留的冲突标记，将自动清理...")
        
        print("\n⚠️  开始解决冲突...")
        print("💡 解决策略: 保留其中一个分支的代码，或合并两个分支的代码")
        print("   如果无法解决，将保留原始冲突标记")
        
        resolved_count = 0
        failed_files = []
        skipped_count = 0
        
        # 解决每个文件的冲突
        for path, sections in conflicts.items():
            full_path = repo_path / path if not Path(path).is_absolute() else Path(path)
            
            print(f"\n🤖 使用 AI 解决冲突: {path}")
            
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            original_conflict_count = content.count("<<<<<<< HEAD")
            
            # 过滤掉不完整的冲突块
            complete_sections = []
            for section in sections:
                if is_complete_conflict(section):
                    complete_sections.append(section)
                else:
                    print(f"   ⚠️  跳过不完整的冲突块（缺少必要的标记）")
                    skipped_count += 1
            
            if not complete_sections:
                print(f"   ⚠️  没有完整的冲突块可以解决，保留原始冲突标记")
                failed_files.append(path)
                continue
            
            # 批量解决冲突
            try:
                resolved_sections = resolve_conflict_sections_batch(
                    complete_sections,
                    model=final_model,
                    max_batch_size=max_batch_size
                )
                cleaned_sections = []
                for i, rs in enumerate(resolved_sections):
                    cleaned = clean_ai_response(rs)
                    if cleaned is None or not validate_resolution(complete_sections[i], cleaned):
                        print(f"   ⚠️  冲突 {i+1} 的解决方案无效，将跳过（保留原始冲突标记）")
                        cleaned_sections.append(None)
                    else:
                        cleaned_sections.append(cleaned)
                
                resolved_map = {}
                for section, cleaned in zip(complete_sections, cleaned_sections):
                    if cleaned is not None:
                        resolved_map[section] = cleaned
                    else:
                        skipped_count += 1
            except Exception as e:
                error_msg = str(e)
                print(f"⚠️  批量解决失败，使用单个解决: {error_msg}")
                
                if "401" in error_msg or "invalid_api_key" in error_msg.lower():
                    print("❌ API key 验证失败")
                    print("💡 请检查:")
                    print("   1. API key 是否正确（使用 JieKou.AI 平台提供的 key）")
                    print("   2. API key 是否已激活")
                    print("   3. 账户是否有余额")
                
                resolved_map = {}
                for sec in complete_sections:
                    try:
                        merged = resolve_conflict_section_single(sec, model=final_model)
                        cleaned = clean_ai_response(merged)
                        if cleaned is None or not validate_resolution(sec, cleaned):
                            print(f"   ⚠️  此冲突的解决方案无效，将跳过（保留原始冲突标记）")
                            skipped_count += 1
                            continue
                        resolved_map[sec] = cleaned
                    except Exception as e2:
                        error_msg2 = str(e2)
                        print(f"❌ 解决单个冲突失败: {error_msg2}")
                        if "401" in error_msg2 or "invalid_api_key" in error_msg2.lower():
                            failed_files.append(path)
                            break
                        skipped_count += 1
                        continue
                
                if not resolved_map:
                    failed_files.append(path)
                    continue
            
            if not resolved_map:
                print(f"   ⚠️  没有有效的解决方案，保留原始冲突标记")
                failed_files.append(path)
                continue
            
            # 应用解决方案
            try:
                final_content, replaced_count = apply_resolutions_safe(str(full_path), content, resolved_map)
                if replaced_count < len(resolved_map):
                    print(f"   ⚠️  只替换了 {replaced_count}/{len(resolved_map)} 个冲突标记")
                if replaced_count == 0:
                    print(f"   ⚠️  未能替换任何冲突，保留原始冲突标记")
                    failed_files.append(path)
                    continue
            except Exception as e:
                print(f"⚠️  使用改进方法失败，回退到原始方法: {e}")
                try:
                    cleaned_resolved_map = {}
                    for k, v in resolved_map.items():
                        cleaned = clean_ai_response(v)
                        if cleaned is not None and validate_resolution(k, cleaned):
                            cleaned_resolved_map[k] = cleaned
                    
                    if cleaned_resolved_map:
                        apply_resolutions(str(full_path), content, cleaned_resolved_map)
                        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                            final_content = f.read()
                        final_content = remove_duplicate_lines(final_content)
                        with open(full_path, 'w', encoding='utf-8') as f:
                            f.write(final_content)
                    else:
                        print(f"   ⚠️  所有解决方案都无效，保留原始冲突标记")
                        failed_files.append(path)
                        continue
                except Exception as e2:
                    print(f"❌ 应用解决方案失败: {e2}")
                    failed_files.append(path)
                    continue
            
            # 验证：检查是否还有冲突标记
            remaining_conflicts = final_content.count("<<<<<<< HEAD")
            if remaining_conflicts > 0:
                if remaining_conflicts < original_conflict_count:
                    print(f"   ℹ️  解决了 {original_conflict_count - remaining_conflicts} 个冲突，还有 {remaining_conflicts} 个冲突未解决（已保留原始冲突标记）")
                else:
                    print(f"   ⚠️  所有冲突都未解决，保留原始冲突标记")
                    failed_files.append(path)
                    continue
            
            # 检测重复代码行
            duplicates = detect_duplicate_code(final_content)
            if duplicates:
                print(f"   ⚠️  检测到重复代码行:")
                for dup in duplicates[:3]:
                    print(f"      第 {dup['line']} 行: {dup['content'][:60]} (重复 {dup['count']} 次)")
                if len(duplicates) > 3:
                    print(f"      ... 还有 {len(duplicates) - 3} 处重复")
                
                print(f"   🔧 尝试清理重复代码行...")
                cleaned_content = remove_duplicate_lines(final_content)
                if cleaned_content != final_content:
                    print(f"   ✅ 已清理重复代码行")
                    final_content = cleaned_content
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(final_content)
            
            # 最终验证：确保已解决的冲突没有残留标记
            if check_conflict_markers(final_content):
                current_conflict_count = final_content.count("<<<<<<< HEAD")
                if current_conflict_count <= original_conflict_count - replaced_count:
                    print(f"   ℹ️  还有 {current_conflict_count} 个冲突未解决（已保留原始冲突标记）")
                else:
                    print(f"   ⚠️  警告: 检测到异常数量的冲突标记")
                    lines = final_content.split('\n')
                    conflict_lines = []
                    for i, line in enumerate(lines, 1):
                        if re.match(r'^<<<<<<<', line) or re.match(r'^=======', line) or re.match(r'^>>>>>>>', line):
                            conflict_lines.append(f"  第 {i} 行: {line[:80]}")
                    
                    if conflict_lines:
                        print(f"   冲突标记位置:")
                        for line_info in conflict_lines[:5]:
                            print(line_info)
                        if len(conflict_lines) > 5:
                            print(f"   ... 还有 {len(conflict_lines) - 5} 个冲突标记")
                    print(f"   ⚠️  保留所有冲突标记（包括未解决的冲突）")
            
            # 使用 git add 命令标记冲突已解决
            stdout, stderr, code = run_git_cmd(repo_path, ["add", path])
            if code != 0:
                print(f"⚠️  添加文件到暂存区失败: {stderr}")
                if "unmerged" in stderr.lower() or "conflict" in stderr.lower():
                    print(f"   ℹ️  文件仍有未解决的冲突，这是正常的（已保留原始冲突标记）")
                failed_files.append(path)
                continue
            
            resolved_count += 1
            print(f"✅ 成功解决: {path}")
        
        if skipped_count > 0:
            print(f"\nℹ️  跳过了 {skipped_count} 个无法解决的冲突（已保留原始冲突标记）")
        
        if failed_files:
            print(f"\n⚠️  以下文件解决失败: {', '.join(failed_files)}")
            print("💡 提示:")
            print("   1. 这些文件可能包含复杂的冲突，需要手动检查")
            print("   2. 未解决的冲突已保留原始冲突标记")
            print("   3. 可以查看临时目录中的文件进行手动修复")
            print(f"   4. 临时目录: {temp_dir}")
        
        if resolved_count == 0:
            print("\n❌ 未能解决任何冲突")
            if skipped_count > 0:
                print(f"   跳过了 {skipped_count} 个冲突（AI 无法提供有效解决方案，已保留原始冲突标记）")
            sys.exit(1)
        
        print(f"\n✅ 成功解决 {resolved_count}/{len(conflicts)} 个文件的冲突")
        if skipped_count > 0:
            print(f"   跳过了 {skipped_count} 个冲突（已保留原始冲突标记）")
        
        # 检查 Git 状态
        stdout, stderr, code = run_git_cmd(repo_path, ["status", "--porcelain"])
        unmerged = [line for line in stdout.split('\n') if line.startswith('UU') or line.startswith('AA')]
        if unmerged:
            print(f"\n⚠️  仍有未合并的文件: {unmerged}")
            print(f"   ℹ️  这些文件可能包含未解决的冲突（已保留原始冲突标记）")
            for path in failed_files:
                print(f"   尝试强制添加: {path}")
                run_git_cmd(repo_path, ["add", "--force", path])
        
        stdout2, stderr2, code2 = run_git_cmd(repo_path, ["status", "--porcelain"])
        remaining_unmerged = [line for line in stdout2.split('\n') if line.startswith('UU') or line.startswith('AA')]
        
        # 提交更改
        print("\n💾 提交更改...")
        commit_message = "chore: resolve merge conflicts using AI"
        if merge_result.returncode == 0:
            commit_message = "chore: clean up conflict markers using AI"
        
        stdout, stderr, code = run_git_cmd(repo_path, ["commit", "-m", commit_message])
        if code != 0:
            stdout3, stderr3, code3 = run_git_cmd(repo_path, ["status", "--porcelain"])
            if not stdout3.strip():
                print("ℹ️  没有需要提交的更改")
            else:
                print(f"❌ 提交失败: {stderr}")
                if remaining_unmerged:
                    print(f"⚠️  仍有未合并的文件，可能需要手动解决: {remaining_unmerged}")
                    print(f"   ℹ️  未解决的冲突已保留原始冲突标记")
                stdout4, stderr4, code4 = run_git_cmd(repo_path, ["status"])
                print(f"📋 Git 状态:\n{stdout4}")
                print("⚠️  继续尝试推送已解决的文件...")
        
        # 推送到远程
        print("\n🚀 推送到远程仓库...")
        push_url = f"https://{token}@github.com/{owner}/{repo_name}.git"
        stdout, stderr, code = run_git_cmd(repo_path, ["remote", "set-url", "origin", push_url])
        if code != 0:
            print(f"⚠️  设置远程 URL 失败: {stderr}")
        
        stdout, stderr, code = run_git_cmd(repo_path, ["push", "origin", pr_info['head_ref']])
        if code != 0:
            print(f"❌ 推送失败: {stderr}")
            if remaining_unmerged:
                print(f"💡 提示: 仍有未合并的文件，可能需要先手动解决这些冲突")
                print(f"   未合并的文件: {remaining_unmerged}")
                print(f"   ℹ️  未解决的冲突已保留原始冲突标记")
            sys.exit(1)
        
        if failed_files:
            print("\n⚠️  部分文件解决失败，但已推送已解决的文件")
            print(f"   失败的文件: {', '.join(failed_files)}")
            print(f"   未解决的冲突已保留原始冲突标记")
            print(f"   请手动检查并修复这些文件")
        else:
            print("\n🎉 完成！所有冲突已解决并推送到 PR 分支")
        
        print(f"\nPR: https://github.com/{owner}/{repo_name}/pull/{pr_number}")
        
    finally:
        pass


if __name__ == "__main__":
    main()