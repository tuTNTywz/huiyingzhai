# python -m app：读取 .env 后启动服务（systemd 使用此入口）
import uvicorn

from app.config import config


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=config.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
