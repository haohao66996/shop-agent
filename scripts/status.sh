#!/bin/bash
# shop-agent 一键体检: 双模型 / API / 前端 / GPU / 磁盘
cd /root/autodl-tmp/shop_agent
ok() { curl -s --max-time 2 "$1" >/dev/null 2>&1 && echo "✓ 运行" || echo "✗ 未响应"; }
echo "────── 服务状态 ──────"
echo "vLLM 7B  (8000 Planner) : $(ok http://127.0.0.1:8000/v1/models)"
echo "vLLM 14B (8001 Analyst) : $(ok http://127.0.0.1:8001/v1/models)"
echo "API     (8100 后端)     : $(ok http://127.0.0.1:8100/api/health)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:8100/app/)
[ "$CODE" = "200" ] && echo "前端     /app           : ✓ 可访问" || echo "前端     /app           : ✗ ($CODE)"
echo "────── 资源 ──────"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null \
  | awk '{printf "GPU 显存 : %s / %s\n", $1, $2}'
df -h /root/autodl-tmp | tail -1 | awk '{printf "数据盘   : %s 已用 / %s（%s）\n", $3, $2, $5}'
echo "────── 快速自检命令 ──────"
echo "回归测试: /root/autodl-tmp/envs/shop_agent/bin/python scripts/regression.py"
echo "实时日志: tail -f logs/api.log"
