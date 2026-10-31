"""把 WSL 里的 PostgreSQL 暴露成 Windows 本地端口（无需管理员）。

背景（2026-10-31）：PG18 按「wsl 启动数据库服务」的口径装在 WSL 里，而本机的
WSL 网络是 mirrored 模式——Windows → WSL 的 loopback 直连实测被拒（Hyper-V
防火墙默认入站拦截，放行需要管理员），admin 免改方案只剩走 **wsl.exe interop
的 stdio 通道**：wsl.exe 进程在 Windows 侧是普通子进程，stdin/stdout 与 WSL
内部命令的 stdin/stdout 直通（整条会话都在用的通道，与网络模式无关）。

原理：Windows 侧监听 127.0.0.1:<listen-port>，每来一条连接就拉起一个
`wsl -d <distro> -e socat - TCP:127.0.0.1:<pg-port>`，把套接字与子进程的
stdin/stdout 对拷。PG 是每连接 fork 的进程模型，一条连接一个 socat 的成本
（约 100-300ms 的 wsl.exe 启动）由连接池摊薄，开发/测试场景无感。

用法：
    python scripts/pg_wsl_relay.py            # 127.0.0.1:15432 -> WSL 127.0.0.1:5433
    python scripts/pg_wsl_relay.py --listen 25432 --pg-port 5433 --distro Ubuntu-22.04

配套环境（KK_* 仓规）：
    KK_DB_URL=postgresql://kk:<pass>@127.0.0.1:15432/<db>
"""
import argparse
import asyncio
import sys


async def pipe(src: asyncio.StreamReader, dst_writer: asyncio.StreamWriter):
    try:
        while chunk := await src.read(65536):
            dst_writer.write(chunk)
            await dst_writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst_writer.close()
        except (ConnectionError, RuntimeError):
            pass


async def handle(client_reader: asyncio.StreamReader,
                 client_writer: asyncio.StreamWriter,
                 wsl_args: list):
    proc = await asyncio.create_subprocess_exec(
        *wsl_args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    await asyncio.gather(
        pipe(client_reader, proc.stdin.writer if hasattr(proc.stdin, "writer") else proc.stdin),
        pipe(proc.stdout, client_writer),
    )


async def main():
    ap = argparse.ArgumentParser(description="WSL PG -> Windows 本地端口转发（wsl.exe stdio 通道）")
    ap.add_argument("--listen", type=int, default=15432, help="Windows 侧监听端口")
    ap.add_argument("--pg-port", type=int, default=5433, help="WSL 侧 PG 端口")
    ap.add_argument("--distro", default="Ubuntu-22.04", help="WSL 发行版名")
    args = ap.parse_args()

    wsl_args = ["wsl", "-d", args.distro, "-e", "socat", "-",
                f"TCP:127.0.0.1:{args.pg_port}"]

    async def handler(r, w):
        try:
            await handle(r, w, wsl_args)
        finally:
            w.close()

    server = await asyncio.start_server(handler, "127.0.0.1", args.listen)
    print(f"relay ready: 127.0.0.1:{args.listen} -> WSL({args.distro}) "
          f"127.0.0.1:{args.pg_port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
