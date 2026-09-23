#!/bin/bash
# shop-agent 一键启动（AutoDL 无 systemd 版）: vLLM + FastAPI
set -e
cd /root/autodl-tmp/shop_agent
# 环境: 优先 conda(旧机布局)；否则直接用数据盘 venv(新机布局, 无 miniconda)
if [ -f /root/miniconda3/bin/activate ]; then
  source /root/miniconda3/bin/activate shop_agent
else
  export PATH="/root/autodl-tmp/envs/shop_agent/bin:$PATH"
fi
# flashinfer JIT 编译需要 CUDA≥12 的 nvcc：兼容三种布局(cu13 工具链/cu12 pip nvcc/系统 CUDA)
SITE_DIR="$(python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
for NVCC_BIN in "$SITE_DIR/nvidia/cu13/bin" "$SITE_DIR/nvidia/cuda_nvcc/bin" /usr/local/cuda/bin; do
  [ -x "$NVCC_BIN/nvcc" ] && export PATH="$NVCC_BIN:$PATH" && export CUDA_HOME="${NVCC_BIN%/bin}" && break
done
# flashinfer 0.6.16 的 JIT 采样算子与本机 cu13 wheel 头文件不兼容 → 禁用其采样器(用 torch 原生采样)
export VLLM_USE_FLASHINFER_SAMPLER=0
set -a; [ -f .env ] && . ./.env; set +a
mkdir -p logs

# 1) vLLM（已存活则跳过）
if ! curl -s --max-time 2 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  echo "[vllm] 启动: ${VLLM_MODEL_PATH}"
  nohup vllm serve "${VLLM_MODEL_PATH:?请在 .env 配置 VLLM_MODEL_PATH}" \
    --served-model-name "${VLLM_SERVED_NAME:-glm-4-9b}" \
    --port 8000 --gpu-memory-utilization 0.35 --max-model-len 8192 --max-num-seqs 16 \
    > logs/vllm.log 2>&1 &
  echo "[vllm] 等待就绪(首次需加载权重 1-2 分钟)..."
  for i in $(seq 1 120); do
    curl -s --max-time 2 http://127.0.0.1:8000/v1/models >/dev/null 2>&1 && break
    sleep 5
  done
  curl -s http://127.0.0.1:8000/v1/models >/dev/null 2>&1 \
    && echo "[vllm] 就绪 ✓" || { echo "[vllm] 启动失败，见 logs/vllm.log"; exit 1; }
else
  echo "[vllm] 已在运行 ✓"
fi

# 1b) 第二个 vLLM 实例（14B，给 Analyst 用；模型分层路由）
if [ -n "${VLLM2_MODEL_PATH:-}" ] && ! curl -s --max-time 2 http://127.0.0.1:8001/v1/models >/dev/null 2>&1; then
  nohup vllm serve "${VLLM2_MODEL_PATH}" \
    --served-model-name "${VLLM2_SERVED_NAME:-qwen2.5-14b-awq}" \
    --port 8001 --gpu-memory-utilization 0.50 --max-model-len 8192 --max-num-seqs 8 \
    > logs/vllm2.log 2>&1 &
  echo "[vllm2] 后台启动中(Analyst 模型): ${VLLM2_MODEL_PATH}"
fi

# 2) FastAPI (8100)
if ! curl -s --max-time 2 http://127.0.0.1:8100/api/health >/dev/null 2>&1; then
  nohup python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8100 \
    > logs/api.log 2>&1 &
  sleep 2
  echo "[api]   http://0.0.0.0:8100 ✓"
else
  echo "[api]   已在运行 ✓"
fi

echo "全部启动完成。日志: logs/{vllm,api}.log"
