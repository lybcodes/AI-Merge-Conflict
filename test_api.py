#!/usr/bin/env python3
# test_api.py - 测试 API 连接和余额

import os
import sys

try:
    from openai import OpenAI
except ImportError:
    print("❌ 需要安装 openai: pip install openai")
    sys.exit(1)

# 获取配置
api_key = os.getenv("OPENAI_API_KEY")
api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")

if not api_key:
    print("❌ 错误: 需要设置 OPENAI_API_KEY 环境变量")
    sys.exit(1)

print(f"🔍 测试 API 连接...")
print(f"   API Key: {api_key[:10]}...")
print(f"   API Base: {api_base}")

# 确保 API base 格式正确
if not api_base.endswith('/v1'):
    if api_base.endswith('/'):
        api_base = api_base + 'v1'
    else:
        api_base = api_base + '/v1'

print(f"   修正后的 API Base: {api_base}")

try:
    # 创建客户端
    client = OpenAI(api_key=api_key, base_url=api_base)
    
    # 测试调用（使用最小的请求）
    print("\n🧪 发送测试请求...")
    response = client.chat.completions.create(
        model="gpt-4o",  # 使用便宜的模型测试
        messages=[
            {"role": "user", "content": "Hello"}
        ],
        max_tokens=5
    )
    
    print("✅ API 连接成功！")
    print(f"   响应: {response.choices[0].message.content}")
    print(f"   使用的 tokens: {response.usage.total_tokens}")
    
except Exception as e:
    error_msg = str(e)
    print(f"\n❌ API 调用失败:")
    print(f"   错误: {error_msg}")
    
    # 常见错误提示
    if "insufficient_quota" in error_msg or "billing" in error_msg.lower():
        print("\n💡 提示: API key 余额不足或未充值")
        print("   请检查:")
        print("   1. API key 是否正确")
        print("   2. 账户是否有余额")
        print("   3. 是否需要充值")
    elif "401" in error_msg or "unauthorized" in error_msg.lower():
        print("\n💡 提示: API key 无效或未授权")
        print("   请检查 API key 是否正确")
    elif "404" in error_msg or "not found" in error_msg.lower():
        print("\n💡 提示: API 地址不正确")
        print(f"   当前地址: {api_base}")
        print("   请检查代理服务的文档，确认正确的 API 地址格式")
    elif "connection" in error_msg.lower() or "timeout" in error_msg.lower():
        print("\n💡 提示: 网络连接问题")
        print("   请检查:")
        print("   1. 网络连接是否正常")
        print("   2. API 地址是否可以访问")
        print("   3. 是否需要代理")
    
    sys.exit(1)