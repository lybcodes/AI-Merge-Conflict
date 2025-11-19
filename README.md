# AI-Powered GitHub PR Conflict Resolver

一个基于 AI 的 GitHub Pull Request 代码冲突自动解决工具。使用 `reconcile-ai` 和 OpenAI API 自动检测、分析和解决 PR 中的合并冲突。

## ✨ 功能特性

- 🤖 **AI 自动解决冲突**：使用 GPT-4o 等大语言模型智能解决代码冲突
- 🔧 **支持自定义 API**：支持使用国内代理服务（如 JieKou.AI）访问 OpenAI API
- 📝 **批量处理**：自动检测并批量解决多个文件的冲突
- ✅ **自动提交推送**：解决冲突后自动提交并推送到 PR 分支

## 📋 前置要求

- Python 3.8+
- Git
- GitHub Personal Access Token
- OpenAI API Key（或支持 OpenAI 兼容 API 的代理服务）

## 🚀 快速开始

### 1. 安装依赖

```bash
# 创建虚拟环境（推荐）
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install reconcile-ai requests GitPython
```

### 2. 配置环境变量

```bash
# GitHub Token（必需）
export GITHUB_TOKEN="ghp_your_github_token_here"

# OpenAI API Key（必需）
export OPENAI_API_KEY="sk-your_openai_api_key_here"

# 自定义 API 地址（可选，用于国内代理）
export OPENAI_API_BASE="https://api.jiekou.ai/openai"

# 使用的模型（可选，默认: gpt-4o）
export RECONCILE_MODEL="gpt-4o"
```

### 3. 运行工具

```bash
python resolve_pr_conflicts.py https://github.com/owner/repo/pull/123
```

## 📖 详细使用说明

### 基本用法

```bash
# 使用 PR URL
python resolve_pr_conflicts.py https://github.com/lybcodes/tidb/pull/1
```

### 环境变量说明

| 变量名 | 必需 | 说明 | 示例 |
|--------|------|------|------|
| `GITHUB_TOKEN` | ✅ | GitHub Personal Access Token | `ghp_xxxxxxxxxxxx` |
| `OPENAI_API_KEY` | ✅ | OpenAI API Key | `sk-xxxxxxxxxxxx` |
| `OPENAI_API_BASE` | ❌ | 自定义 API 地址（用于代理） | `https://api.jiekou.ai/openai` |
| `RECONCILE_MODEL` | ❌ | 使用的 AI 模型 | `gpt-4o`, `gpt-4`, `gpt-3.5-turbo` |


### 使用国内代理服务

如果你无法直接访问 OpenAI API，可以使用国内代理服务（如 JieKou.AI）：

```bash
# 设置代理服务的 API 地址
export OPENAI_API_BASE="https://api.jiekou.ai/openai"

# 使用代理服务提供的 API Key
export OPENAI_API_KEY="从代理服务平台获取的key"
```

**注意**：确保使用代理服务**平台提供的 API Key**，而不是 OpenAI 官方的 Key。

## 🔍 工作流程

工具执行以下步骤：

1. **测试 API 连接**：验证 API Key 和模型是否可用
2. **获取 PR 信息**：从 GitHub API 获取 PR 详情
3. **克隆仓库**：使用优化的浅克隆方式快速克隆仓库
4. **检测冲突**：尝试合并并检测是否存在冲突
5. **AI 解决冲突**：使用 AI 分析并解决每个冲突文件
6. **验证结果**：确保所有冲突标记已移除
7. **提交推送**：自动提交并推送到 PR 分支

## 📝 示例输出

ℹ️  使用自定义 API 地址: https://api.jiekou.ai/openai
   实际 API 地址: https://api.jiekou.ai/openai
🧪 测试 API 连接...
   测试模型: gpt-4o
   API 地址: https://api.jiekou.ai/openai
✅ API 连接成功，使用的模型: gpt-4o
✅ API 连接正常
🔍 获取 PR 信息: lybcodes/tidb#1
📋 PR 信息:
  标题: test: add test changes for conflict resolution
  源分支: test-conflict-resolution
  目标分支: master
⚠️  PR 标记为不可合并，可能已有冲突
   合并状态: dirty
📁 临时目录: /var/folders/.../tmp_xxxxx
📥 克隆仓库（优化模式）...
🔀 切换到 PR 分支: test-conflict-resolution
📥 获取目标分支: master
🔀 尝试合并 master 到 test-conflict-resolution
⚠️  检测到合并冲突，开始解决...
🤖 使用模型: gpt-4o, 批量大小: 5
🌐 API 地址: https://api.jiekou.ai/openai
📝 找到 1 个冲突，分布在 1 个文件中
🤖 使用 AI 解决冲突: README.md
✅ 成功解决: README.md
✅ 成功解决 1/1 个文件的冲突
💾 提交更改...
🚀 推送到远程仓库...
🎉 完成！冲突已解决并推送到 PR 分支
PR: https://github.com/lybcodes/tidb/pull/1