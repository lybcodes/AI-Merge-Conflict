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
    
    # 确保父目录存在
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 方法 1: 使用浅克隆 + 单分支（最快）
    # 只克隆需要的分支，深度为 1（只获取最新提交）
    clone_cmd = [
        "git", "clone",
        "--depth", "1",  # 浅克隆，只获取最新提交
        "--single-branch",  # 只克隆一个分支
        "--branch", head_ref,  # 克隆 PR 分支
        repo_url,
        str(repo_path)  # 使用完整路径
    ]
    
    # 在父目录执行克隆命令
    result = subprocess.run(
        clone_cmd,
        cwd=str(repo_path.parent),
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        # 如果 PR 分支不存在，尝试克隆 base 分支然后切换
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
        
        # 切换到 PR 分支
        print(f"🔀 切换到 PR 分支: {head_ref}")
        stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{head_ref}:{head_ref}", "--depth", "1"])
        if code != 0:
            # 尝试不限制深度
            stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{head_ref}:{head_ref}"])
            if code != 0:
                print(f"⚠️  获取 PR 分支失败: {stderr}")
                return False
        
        stdout, stderr, code = run_git_cmd(repo_path, ["checkout", head_ref])
        if code != 0:
            print(f"❌ 切换分支失败: {stderr}")
            return False
    
    # 获取 base 分支（用于合并）
    # 注意：浅克隆时，需要确保两个分支有共同的历史
    # 如果 base 分支和 head 分支不共享历史，需要增加深度或使用 --unshallow
    print(f"📥 获取目标分支: {base_ref}")
    
    # 先尝试浅克隆获取
    stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{base_ref}:origin/{base_ref}", "--depth", "10"])
    if code != 0:
        print(f"⚠️  浅克隆获取目标分支失败，尝试增加深度...")
        # 增加深度以获取更多历史
        stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", f"{base_ref}:origin/{base_ref}", "--depth", "50"])
        if code != 0:
            print(f"⚠️  增加深度后仍失败，尝试完整获取...")
            # 尝试不限制深度
            stdout, stderr, code = run_git_cmd(repo_path, ["fetch", "origin", base_ref])
            if code != 0:
                print(f"❌ 获取目标分支失败: {stderr}")
                return False
    
    return True


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
        
        if merge_result.returncode == 0:
            print("✅ PR 无冲突，无需解决")
            return
        
        # 检查是否有冲突
        stderr = merge_result.stderr
        stdout = merge_result.stdout
        
        if "CONFLICT" not in stderr and "冲突" not in stderr and "CONFLICT" not in stdout:
            # 检查是否是 unrelated histories 错误
            if "unrelated histories" in stderr.lower():
                print(f"⚠️  检测到不相关历史，已使用 --allow-unrelated-histories")
                # 如果已经使用了 --allow-unrelated-histories 还是失败，可能是其他问题
                print(f"❌ 合并失败: {stderr}")
                sys.exit(1)
            
            print(f"❌ 合并失败，但未检测到冲突标记")
            print(f"   退出码: {merge_result.returncode}")
            print(f"   错误信息: {stderr if stderr else '(无错误信息)'}")
            sys.exit(1)
        
        print("⚠️  检测到合并冲突，开始解决...")
        
        # 确保环境变量设置
        if api_base:
            os.environ['OPENAI_API_BASE'] = api_base
        
        # 使用 reconcile-ai 解决冲突
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
        
        # 检测冲突
        blobs = detect_conflicts(str(repo_path))
        if not blobs:
            print("❌ 未找到冲突文件")
            sys.exit(1)
        
        # 解析冲突
        conflicts = parse_conflicts(blobs, str(repo_path))
        git_repo = Repo(str(repo_path))
        
        total_conflicts = sum(len(sections) for sections in conflicts.values())
        print(f"📝 找到 {total_conflicts} 个冲突，分布在 {len(conflicts)} 个文件中")
        
        if total_conflicts == 0:
            print("ℹ️  未找到冲突内容")
            sys.exit(0)
        
        resolved_count = 0
        failed_files = []
        
        # 解决每个文件的冲突
        for path, sections in conflicts.items():
            full_path = repo_path / path if not Path(path).is_absolute() else Path(path)
            
            print(f"🤖 使用 AI 解决冲突: {path}")
            
            with open(full_path, 'r') as f:
                content = f.read()
            
            # 批量解决冲突
            try:
                resolved_sections = resolve_conflict_sections_batch(
                    sections,
                    model=final_model,
                    max_batch_size=max_batch_size
                )
                resolved_map = dict(zip(sections, resolved_sections))
            except Exception as e:
                error_msg = str(e)
                print(f"⚠️  批量解决失败，使用单个解决: {error_msg}")
                
                if "401" in error_msg or "invalid_api_key" in error_msg.lower():
                    print("❌ API key 验证失败")
                    print("💡 请检查:")
                    print("   1. API key 是否正确（使用 JieKou.AI 平台提供的 key）")
                    print("   2. API key 是否已激活")
                    print("   3. 账户是否有余额")
                
                # 单个解决
                resolved_map = {}
                all_resolved = True
                for sec in sections:
                    try:
                        merged = resolve_conflict_section_single(sec, model=final_model)
                        resolved_map[sec] = merged
                    except Exception as e2:
                        error_msg2 = str(e2)
                        print(f"❌ 解决单个冲突失败: {error_msg2}")
                        if "401" in error_msg2 or "invalid_api_key" in error_msg2.lower():
                            all_resolved = False
                            failed_files.append(path)
                            break
                        all_resolved = False
                        failed_files.append(path)
                        break
                
                if not all_resolved:
                    continue
            
            # 验证解决结果
            test_content = content
            for section, resolved in resolved_map.items():
                test_content = test_content.replace(section, resolved)
            
            if "<<<<<<< HEAD" in test_content or "=======" in test_content or ">>>>>>>" in test_content:
                print(f"⚠️  警告: {path} 解决后仍有冲突标记，跳过")
                failed_files.append(path)
                continue
            
            # 应用解决方案
            apply_resolutions(str(full_path), content, resolved_map)
            
            # 验证文件确实没有冲突标记了
            with open(full_path, 'r') as f:
                final_content = f.read()
                if "<<<<<<< HEAD" in final_content or "=======" in final_content or ">>>>>>>" in final_content:
                    print(f"⚠️  警告: {path} 应用后仍有冲突标记")
                    failed_files.append(path)
                    continue
            
            # 使用 git add 命令标记冲突已解决
            stdout, stderr, code = run_git_cmd(repo_path, ["add", path])
            if code != 0:
                print(f"⚠️  添加文件到暂存区失败: {stderr}")
                failed_files.append(path)
                continue
            
            resolved_count += 1
            print(f"✅ 成功解决: {path}")
        
        if failed_files:
            print(f"⚠️  以下文件解决失败: {', '.join(failed_files)}")
        
        if resolved_count == 0:
            print("❌ 未能解决任何冲突")
            sys.exit(1)
        
        print(f"✅ 成功解决 {resolved_count}/{len(conflicts)} 个文件的冲突")
        
        # 检查 Git 状态
        stdout, stderr, code = run_git_cmd(repo_path, ["status", "--porcelain"])
        unmerged = [line for line in stdout.split('\n') if line.startswith('UU') or line.startswith('AA')]
        if unmerged:
            print(f"⚠️  仍有未合并的文件: {unmerged}")
            # 强制添加所有冲突文件
            for path in conflicts.keys():
                run_git_cmd(repo_path, ["add", path])
        
        # 提交更改
        print("💾 提交更改...")
        stdout, stderr, code = run_git_cmd(repo_path, ["commit", "-m", "chore: resolve merge conflicts using AI"])
        if code != 0:
            print(f"❌ 提交失败: {stderr}")
            # 查看 Git 状态
            stdout3, stderr3, code3 = run_git_cmd(repo_path, ["status"])
            print(f"📋 Git 状态:\n{stdout3}")
            sys.exit(1)
        
        # 推送到远程
        print("🚀 推送到远程仓库...")
        push_url = f"https://{token}@github.com/{owner}/{repo_name}.git"
        stdout, stderr, code = run_git_cmd(repo_path, ["remote", "set-url", "origin", push_url])
        if code != 0:
            print(f"⚠️  设置远程 URL 失败: {stderr}")
        
        stdout, stderr, code = run_git_cmd(repo_path, ["push", "origin", pr_info['head_ref']])
        if code != 0:
            print(f"❌ 推送失败: {stderr}")
            sys.exit(1)
        
        print("🎉 完成！冲突已解决并推送到 PR 分支")
        print(f"PR: https://github.com/{owner}/{repo_name}/pull/{pr_number}")
        
    finally:
        pass


if __name__ == "__main__":
    main()