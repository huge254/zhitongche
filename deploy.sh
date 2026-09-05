#!/bin/bash
# 职通车·求职招聘平台 —— 完整 Kubernetes（kubeadm 集群）一键部署脚本
# 顺序：PV → MySQL → 种子数据 → ES/Redis → metrics-server(HPA 前置) → 后端 → 前端
# （后端启动时会把 MySQL 中的职位同步进 ES，所以种子数据必须先于后端就绪）
set -e
NS=zhitongche
cd "$(dirname "$0")"

echo "==> [1/9] 检查集群网络插件（Calico）"
if ! kubectl -n kube-system get ds calico-node >/dev/null 2>&1; then
  echo "未检测到 calico-node，应用 k8s/calico.yaml（需已导入 calico 镜像）"
  kubectl apply -f k8s/calico.yaml
  kubectl -n kube-system wait --for=condition=ready pod -l k8s-app=calico-node --timeout=180s || \
    echo "警告：calico 未完全就绪，请检查后再继续"
else
  echo "calico-node 已存在，跳过"
fi

echo "==> [2/9] 创建命名空间"
kubectl create ns $NS --dry-run=client -o yaml | kubectl apply -f -

echo "==> [3/9] 静态 PV（hostPath，完整 K8s 无内置存储供应）"
kubectl -n $NS apply -f k8s/hostpath-pv.yaml

echo "==> [4/9] 建表脚本"
kubectl -n $NS apply -f k8s/init-sql.yaml

echo "==> [5/9] MySQL（StatefulSet + PVC）"
kubectl -n $NS apply -f k8s/mysql-sts.yaml
echo "等待 PVC 绑定..."
for i in $(seq 1 30); do
  phase=$(kubectl -n $NS get pvc mysql-data-mysql-0 -o jsonpath='{.status.phase}' 2>/dev/null)
  [ "$phase" = "Bound" ] && break
  sleep 2
done
if [ "$phase" != "Bound" ]; then
  echo "!! PVC 未绑定，诊断信息如下："
  kubectl -n $NS get pvc
  kubectl get pv
  kubectl -n $NS describe sts mysql | tail -15
  exit 1
fi
kubectl -n $NS wait --for=condition=ready pod -l app=mysql --timeout=300s

echo "==> [6/9] 种子数据（3 家企业 + 8 个职位）"
kubectl -n $NS delete job ztc-seed --ignore-not-found
kubectl -n $NS apply -f k8s/seed-job.yaml
kubectl -n $NS wait --for=condition=complete job/ztc-seed --timeout=180s

echo "==> [7/9] Elasticsearch + Redis"
kubectl -n $NS apply -f k8s/es.yaml
kubectl -n $NS apply -f k8s/redis.yaml
echo "等待 ES PVC 绑定..."
for i in $(seq 1 30); do
  phase=$(kubectl -n $NS get pvc es-data-elasticsearch-0 -o jsonpath='{.status.phase}' 2>/dev/null)
  [ "$phase" = "Bound" ] && break
  sleep 2
done
if [ "$phase" != "Bound" ]; then
  echo "!! ES PVC 未绑定，诊断信息如下："
  kubectl -n $NS get pvc
  kubectl get pv
  kubectl -n $NS describe sts elasticsearch | tail -15
  exit 1
fi
kubectl -n $NS wait --for=condition=ready pod -l app=elasticsearch --timeout=300s
kubectl -n $NS wait --for=condition=ready pod -l app=redis --timeout=120s

echo "==> [8/9] metrics-server（HPA 前置，若已装会自动跳过）"
if ! kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1; then
  if [ -f metrics-server-components_v0.9.0.yaml ]; then
    kubectl apply -f metrics-server-components_v0.9.0.yaml
    echo "metrics-server 已部署，等待就绪（约 30~60 秒）"
    kubectl -n kube-system wait --for=condition=ready pod -l k8s-app=metrics-server --timeout=120s || \
      echo "提示：metrics-server 就绪较慢也不阻塞，HPA 稍后可用"
  else
    echo "警告：未找到 metrics-server-components_v0.9.0.yaml，HPA 将显示 unknown（不影响部署）"
  fi
else
  echo "metrics-server 已存在，跳过"
fi

echo "==> [9/9] 后端（Deployment + HPA）"
kubectl -n $NS apply -f k8s/backend.yaml
kubectl -n $NS wait --for=condition=ready pod -l app=backend --timeout=300s

echo "==> [10/10] 前端（Nginx + NodePort 30080）"
kubectl -n $NS apply -f k8s/frontend.yaml
kubectl -n $NS wait --for=condition=ready pod -l app=frontend --timeout=120s

echo ""
echo "==> 部署完成，状态总览："
kubectl -n $NS get pods
kubectl -n $NS get hpa

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "访问地址：http://${IP}:30080"
echo "HPA 压测：for i in \$(seq 1 50); do curl -s http://localhost:30080/api/stress?seconds=5 >/dev/null & done"
echo "观察扩容：kubectl -n $NS get hpa -w"
