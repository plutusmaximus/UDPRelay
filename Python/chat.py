import argparse
from server import udp_group_broadcast_server, MAX_PAYLOAD
from client import run_udp_client_cli


def main():
    """Entry point for UDPRelay: run as server or client."""
    parser = argparse.ArgumentParser(description="UDPRelay - Server/Client")
    parser.add_argument("--server", action="store_true", help="Run as server (otherwise client)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Server mode: bind host. Client mode: server host."
    )
    parser.add_argument("--port", type=int, default=5000, help="UDP port")
    parser.add_argument("--empty-ttl", type=float, default=300.0, help="Seconds until empty groups expire")
    parser.add_argument("--sweep", type=float, default=30.0, help="Sweep interval")
    parser.add_argument("--heartbeat", type=float, default=60.0, help="Suggested heartbeat seconds")
    parser.add_argument("--cap", type=int, default=128, help="Max group size (None to disable)", nargs="?")
    parser.add_argument("--max-groups-per-client", type=int, default=3, help="Max active groups per client")
    args = parser.parse_args()

    if args.server:
        print(f"[udprelay] starting server on {args.host}:{args.port}")
        try:
            udp_group_broadcast_server(
                host=args.host,
                port=args.port,
                bufsize=MAX_PAYLOAD,
                empty_group_ttl_sec=args.empty_ttl,
                sweep_interval_sec=args.sweep,
                suggested_heartbeat_sec=args.heartbeat,
                max_group_size=args.cap,
                per_group_caps=None,
                max_groups_per_client=args.max_groups_per_client,
            )
        except KeyboardInterrupt:
            print("\n[udprelay] server stopped")
    else:
        print(f"[udprelay] starting client; server={args.host}:{args.port}")
        run_udp_client_cli(server_host=args.host, server_port=args.port)


if __name__ == "__main__":
    main()
