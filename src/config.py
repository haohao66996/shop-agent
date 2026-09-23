# -*- coding: utf-8 -*-
"""全局配置：.env 驱动，路径统一解析到项目根目录"""
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # LLM provider 链：逗号分隔，依次降级。可用前缀: vllm / deepseek
    llm_provider_chain: str = "vllm:glm-4-9b,deepseek:deepseek-chat"
    vllm_base_url: str = "http://127.0.0.1:8000/v1"
    vllm_model_name: str = "glm-4-9b"
    vllm_api_key: str = "EMPTY"
    # 第二个 vLLM 实例（可选）：模型分层——Analyst 用更强的模型
    vllm2_base_url: str = "http://127.0.0.1:8001/v1"
    vllm2_api_key: str = "EMPTY"
    analyst_provider: str = ""          # 如 "vllm2:qwen2.5-14b-awq"；空=走全局链
    reporter_provider: str = ""         # Reporter 角色路由（P2-2：提升摘要质量）
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    deepseek_model_name: str = "deepseek-chat"

    database_path: str = "data/shop_agent.db"
    upload_dir: str = "data/uploads"
    task_dir: str = "data/tasks"

    sandbox_timeout_seconds: int = 35
    sandbox_mem_mb: int = 768
    analyst_max_attempts: int = 3
    max_upload_mb: int = 20
    embedding_model_path: str = "/root/autodl-tmp/models_hub/bge-small-zh-v1.5"

    # 演示用简单令牌鉴权；allow_no_auth=True 时跳过（局域网 demo）
    api_token: str = "demo-token"
    allow_no_auth: bool = True

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"          # .env 里 vLLM 启动参数等非本类变量直接忽略


settings = Settings()


def abs_path(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else BASE_DIR / path


for _d in (
    abs_path(settings.upload_dir),
    abs_path(settings.task_dir),
    abs_path(settings.database_path).parent,
    BASE_DIR / "logs",
):
    _d.mkdir(parents=True, exist_ok=True)
