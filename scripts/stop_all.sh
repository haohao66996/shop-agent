#!/bin/bash
cd /root/autodl-tmp/shop_agent
pkill -f "uvicorn src.api.main" 2>/dev/null && echo "[api] stopped"
# vLLM 默认保留（加载慢）；确认要停再执行: pkill -f "vllm serve"
echo "done"
