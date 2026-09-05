#!/bin/bash
# 把项目三所需的全部镜像分发到集群所有节点
# 多节点部署必做：K8s 调度 Pod 到哪台节点，镜像就必须在那台节点上
# 用法：./prepare-images.sh <worker1-ip> <worker2-ip>
# 前提：
#   1. 本机（master）装有 Docker 并已配置镜像加速（/etc/docker/daemon.json）
#   2. 已对两台 worker 配置 root 免密：ssh-keygen + ssh-copy-id（你 Ansible 笔记里做过）
set -e
WORKERS=("$@")
if [ ${#WORKERS[@]} -eq 0 ]; then
  echo "用法: $0 <worker1-ip> <worker2-ip>"
  exit 1
fi

cd "$(dirname "$0")"
TARBALL=/tmp/ztc-images.tar

echo "==> [1/4] 构建业务镜像（首次约 5~10 分钟）"
(cd backend && docker build -t ztc-backend:1.0 .)
(cd frontend && docker build -t ztc-frontend:1.0 .)

echo "==> [2/4] 拉取/确认基础镜像（mysql / redis / elasticsearch）"
docker pull mysql:8.4
docker pull redis:7-alpine
docker pull docker.elastic.co/elasticsearch/elasticsearch:8.13.1 || \
  echo "警告：ES 官方源拉取失败，请手动解决后再运行本脚本（见 README 常见问题）"

echo "==> [3/4] 导出全部镜像为一个 tar 包"
docker save -o "$TARBALL" \
  ztc-backend:1.0 \
  ztc-frontend:1.0 \
  mysql:8.4 \
  redis:7-alpine \
  docker.elastic.co/elasticsearch/elasticsearch:8.13.1

echo "==> [4/4] 本机导入 + 分发到各 worker"
ctr -n k8s.io images import "$TARBALL"
for w in "${WORKERS[@]}"; do
  echo "----> 分发到 $w"
  scp "$TARBALL" "root@$w:/tmp/"
  ssh "root@$w" "ctr -n k8s.io images import /tmp/ztc-images.tar && rm -f /tmp/ztc-images.tar"
done
rm -f "$TARBALL"

echo ""
echo "完成。另需在【每个节点】导入以下本地镜像包（只导入一次）："
echo "  calico-images_v3.32.1.tar.gz            （网络插件，来自微信文件 2026-07）"
echo "  metrics-server_v0.9.0.tar.gz            （HPA 依赖，来自微信文件 2026-07）"
echo "  导入方式：ctr -n k8s.io images import <包名>"
