"""新建本组专用MySQL容器；随机密码仅留在忽略目录，绝不读取日常实例。"""

import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".local-validation" / "group2-mysql"


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-port", action="store_true", help="仅刷新已运行专用容器的随机回环端口")
    args = parser.parse_args()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    settings = PRIVATE / "settings.json"
    if args.refresh_port:
        config = json.loads(settings.read_text("utf-8"))
        if not config["container"].startswith("agenteval-g2-"):
            raise SystemExit("专用容器标识不合法")
        binding = json.loads(run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", config["container"]]).stdout)
        entry = (binding.get("3306/tcp") or [None])[0]
        if not entry or entry["HostIp"] != "127.0.0.1":
            raise SystemExit("专用容器未运行或回环端口不可用")
        previous = f"127.0.0.1:{config['port']}/"
        if previous not in config["url"]:
            raise SystemExit("私有连接配置的端口不一致，拒绝更改")
        config["port"] = int(entry["HostPort"])
        config["url"] = config["url"].replace(previous, f"127.0.0.1:{config['port']}/", 1)
        temporary = settings.with_suffix(".refresh.tmp")
        temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
        temporary.replace(settings)
        print(json.dumps({"status": "PORT_REFRESHED", "container": config["container"], "port": config["port"]}))
        return
    if settings.exists():
        raise SystemExit("本组实例已存在；读取私有配置后显式复用，不重复创建。")
    suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    name = f"agenteval-g2-{suffix}"
    password = secrets.token_urlsafe(32)
    config = {"container": name, "schema": f"agenteval_g2_{suffix}", "user": "agenteval", "password": password, "port": None}
    env_file = PRIVATE / "mysql.env"
    env_file.write_text(f"MYSQL_ROOT_PASSWORD={secrets.token_urlsafe(32)}\nMYSQL_DATABASE={config['schema']}\nMYSQL_USER=agenteval\nMYSQL_PASSWORD={password}\n", encoding="utf-8")
    # 已有镜像，不拉取替换；新独立容器，不绑定日常目录/卷。
    run(["docker", "image", "inspect", "mysql:8.4.13"])
    run(["docker", "run", "--detach", "--name", name, "--env-file", str(env_file), "--publish", "127.0.0.1::3306", "mysql:8.4.13", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_as_cs"])
    binding = json.loads(run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", name]).stdout)
    config["port"] = int(binding["3306/tcp"][0]["HostPort"])
    config["url"] = f"mysql+pymysql://agenteval:{password}@127.0.0.1:{config['port']}/{config['schema']}?charset=utf8mb4"
    settings.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(json.dumps({k: config[k] for k in ("container", "schema", "port")}, indent=2))


if __name__ == "__main__":
    main()
