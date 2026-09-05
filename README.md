# 职通车·求职招聘平台 — 三节点 Kubernetes 部署说明

按《三个练手项目部署方案.md》项目三实现：Flask 后端 + MySQL + **Elasticsearch 全文检索** + Redis 热门缓存，运行在**三节点完整版 Kubernetes（kubeadm + containerd）**：1 控制面（4C6G）+ 2 工作节点（2C2G）。

## 节点规划

| 节点 | 配置 | 角色 | 主机名建议 |
|---|---|---|---|  
| k8s-master | 4C6G | 控制面（也跑业务，已放行调度） | k8s-master |
| k8s-worker1 | 2C2G | 工作节点 | k8s-worker1 |


| k8s-worker2 | 2C2G | 工作节点 | k8s-worker2 |



> ⚠️ 2G 工作节点内存偏紧：项目三组件限额合计 ≈2.2G。K8s 调度器会把负载分散到三个节点（master 也参与调度），单节点最多扛 1~1.5G 业务，可跑。
> 若某节点内存吃紧（OOM），优先把 ES 固定到 master（见文末"进阶调度"），或把 HPA 副本数观察控制在 2。

## 目录结构

```  
zhitongche/   
├── backend/                  # Flask 后端（app.py / Dockerfile / requirements.txt）



├── frontend/                 # 5 个页面 + Nginx 反代 + Dockerfile
├── k8s/                      # 部署清单
│   ├── hostpath-pv.yaml      # 静态 PV（hostPath，多节点注意节点亲和，见文末）
│   ├── init-sql.yaml         # 建表 ConfigMap



│   ├── mysql-sts.yaml        # MySQL StatefulSet + PVC + 探针
│   ├── es.yaml               # ES 单节点调优（关 mmap、堆 512m）
│   ├── redis.yaml            # Redis 限内存 128m + LRU



│   ├── backend.yaml          # 后端 Deployment + 探针 + HPA(1~3 副本)
│   ├── frontend.yaml         # 前端 Deployment + NodePort 30080



│   ├── ingress.yaml          # （进阶）多域名 Ingress



│   └── seed-job.yaml         # 种子数据 Job（3 企业 + 8 职位）
├── metrics-server-components_v0.9.0.yaml   # ← 从微信文件 2026-08\command_file 拷来（HPA 前置）
├── prepare-images.sh         # 多节点镜像分发脚本（必跑）
└── deploy.sh                 # 一键部署脚本
```   

## 第一步：三台虚机基础初始化（每台都做）

复用你《Rocky 系统初始化》的实战经验，三台全部执行：

```bash    
# 主机名（分别在三台执行）
hostnamectl set-hostname k8s-master      # 第二台: k8s-worker1  第三台: k8s-worker2





# /etc/hosts 三台都加（改成你的实际 IP）
cat <<EOF >> /etc/hosts




192.168.x.100 master                
192.168.x.102 node1                 
192.168.x.103 node2                        
EOF                        

# 系统调优（关 swap / SELinux 转 permissive / 关防火墙仅学习环境 / 内核模块与转发）
swapoff -a && sed -i '/swap/s/^/#/' /etc/fstab











setenforce 0 && sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config













systemctl stop firewalld && systemctl disable firewalld














cat <<EOF | tee /etc/modules-load.d/k8s.conf










overlay          
br_netfilter         
EOF         
modprobe overlay && modprobe br_netfilter









cat <<EOF | tee /etc/sysctl.d/k8s.conf





net.bridge.bridge-nf-call-iptables  = 1




net.bridge.bridge-nf-call-ip6tables = 1



net.ipv4.ip_forward                 = 1



EOF  
sysctl --system  

# 时间同步（分布式集群必做，避免证书与日志错乱）
dnf install -y chrony && systemctl enable --now chronyd


```  

## 第二步：三台安装 containerd（每台都做）

用你微信文件里的本地包（`command_file/containerd-2.3.4-linux-amd64.tar.gz`），三台都执行：

```bash 
tar -zxvf containerd-2.3.4-linux-amd64.tar.gz -C /usr/local

mkdir -p /etc/containerd

containerd config default > /etc/containerd/config.toml

sed -i 's|SystemdCgroup = false|SystemdCgroup = true|' /etc/containerd/config.toml

# sandbox 镜像指到阿里云，避免拉 registry.k8s.io 失败（你踩过的坑）
sed -i 's|registry.k8s.io/pause:[0-9.]*|registry.aliyuncs.com/google_containers/pause:3.10.2|' /etc/containerd/config.toml

# systemd 托管（本地包里有 containerd.service，拷到 /etc/systemd/system/）
systemctl daemon-reload && systemctl enable --now containerd
ctr version    # 验证
```

## 第三步：三台安装 kubelet / kubeadm / kubectl（每台都做）

```bash
cat <<EOF | tee /etc/yum.repos.d/kubernetes.repo
[kubernetes]
name=Kubernetes
baseurl=https://mirrors.aliyun.com/kubernetes-new/core/stable/v1.36/rpm/
enabled=1
gpgcheck=0
EOF
dnf install -y kubelet kubeadm kubectl
systemctl enable kubelet
```

## 第四步：master 初始化集群 + worker 加入

```bash
# ---- master 上执行 ----
# 先导入 K8s 组件镜像（本地包：command_file/k8s-v1.36.4-images.tar.gz）
ctr -n k8s.io images import k8s-v1.36.4-images.tar.gz

kubeadm init \
  --kubernetes-version=v1.36.4 \
  --apiserver-advertise-address=<master内网IP> \
  --pod-network-cidr=10.244.0.0/16 \
  --service-cidr=10.96.0.0/12

mkdir -p $HOME/.kube && cp -i /etc/kubernetes/admin.conf $HOME/.kube/config

# 单机/资源紧张场景：允许 master 也跑业务（2C2G worker 扛不动太多，这步很关键）
kubectl taint nodes --all node-role.kubernetes.io/control-plane- || true

# ---- 两台 worker 上执行 ----
# 用 kubeadm init 输出的 join 命令（忘记就回 master 跑：kubeadm token create --print-join-command）
kubeadm join <master内网IP>:6443 --token xxx --discovery-token-ca-cert-hash sha256:xxx

# ---- master 验证 ----
kubectl get nodes    # 三台都 Ready（装完 Calico 之后）
```

## 第五步：安装网络插件（Calico）

```bash
# master 上：导入本地 Calico 镜像（微信文件 2026-07\compressed_file\calico-images_v3.32.1.tar.gz）
ctr -n k8s.io images import calico-images_v3.32.1.tar.gz
# 两个 worker 也要导入（或用下面的分发脚本一起带过去）
# 然后应用 Calico 清单（本地无 yaml 的话，从你 K8s 部署笔记/资料里取 calico.yaml）
kubectl apply -f calico.yaml
kubectl get pods -n kube-system -w    # 等 calico-node 全部 Running，node 变 Ready
```

## 第六步：镜像准备（多节点关键步骤）

```bash
# 1. master 上配置 Docker 镜像加速（构建业务镜像用），复用你项目一配好的 /etc/docker/daemon.json

# 2. 配好到两台 worker 的 root 免密（你 Ansible 笔记里的 ssh-keygen + ssh-copy-id 流程）
ssh-copy-id root@k8s-worker1
ssh-copy-id root@k8s-worker2

# 3. 一键构建 + 分发全部镜像到三台节点
cd ~/zhitongche
./prepare-images.sh k8s-worker1 k8s-worker2

# 4. 每个节点导入 metrics-server 镜像（HPA 依赖）
#    微信文件 2026-07\compressed_file\metrics-server_v0.9.0.tar.gz，三台各执行一次：
ctr -n k8s.io images import metrics-server_v0.9.0.tar.gz

# 5. 把 metrics-server 清单拷到项目目录
cp '/path/to/2026-08/command_file/metrics-server-components_v0.9.0.yaml' ~/zhitongche/
```

## 第七步：一键部署

```bash
cd ~/zhitongche
chmod +x deploy.sh prepare-images.sh
./deploy.sh
```

脚本顺序：**PV → MySQL → 种子数据 → ES/Redis → metrics-server → 后端 → 前端**，每步等就绪再继续。

## 第八步：访问与验证

浏览器打开 `http://<任意节点IP>:30080`（NodePort 全集群可达）：

1. 首页显示 8 个演示职位与热门职位栏
2. 搜索"运维"：命中"运维工程师 / SRE 工程师 / DevOps 工程师"——**全文检索生效的证明**
3. 企业端注册 → 后台审核通过 → 发布职位 → 立即可搜
4. 学生端建简历 → 投递 → 后台流转状态（投递→初筛→面试→录用）

## HPA 压测演示（简历素材）

```bash
kubectl top nodes        # 先确认 metrics-server 就绪

for i in $(seq 1 50); do curl -s http://localhost:30080/api/stress?seconds=5 >/dev/null & done

kubectl -n zhitongche get hpa -w     # 副本 1 → 2~3
kubectl -n zhitongche get pods -w    # 观察 Pod 分布到哪些节点
```

> 2G worker 内存紧：如果扩容时看到 worker 上 Pod 起不来，把 HPA 上限改成 2（`kubectl -n zhitongche edit hpa backend-hpa`，maxReplicas: 2），演示效果一样。

## 进阶调度：把 ES 固定到 master（可选）

2G 节点扛 ES（1G 限额）偏紧，可给 ES 加节点亲和固定到大内存节点：

```yaml
# es.yaml 的 spec.template.spec 下加：
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: kubernetes.io/hostname
                    operator: In
                    values: ["k8s-master"]
``` 

同理 MySQL 也可固定。这样两个 2G 节点只跑后端/前端/种子等轻负载。

## 进阶：Ingress 多域名（可选）

```bash 
kubectl apply -f k8s/ingress.yaml

# 本机 hosts 加： <节点IP> job.local hr.local

``` 

前提：集群已装 ingress-nginx（本地有 `ingress-nginx-4.15.1.tgz` chart + 镜像）。


## 资源预算

| 组件 | limits | 建议落点 |
|---|---|---| 
| Elasticsearch | 1Gi | master（6G） |

| MySQL | 512Mi | master |

| 后端 ×1~3 | 256Mi/副本 | 分散三节点 |
| 前端 / Redis | 128Mi / 192Mi | worker |


## 常见问题

| 现象 | 处理 |
|---|---| 
| node 一直 NotReady | Calico 没起好：`kubectl get pods -n kube-system`，确认三个节点的 calico-node 都 Running；镜像没导全会 ImagePullBackOff |
| Pod 卡在 Pending | `kubectl describe pod` 看事件；多为 PVC 未绑定（hostpath-pv.yaml 是否 apply）或节点内存不足 |
| ImagePullBackOff | 镜像没分发到该节点：重跑 `./prepare-images.sh`，或手动 `ctr -n k8s.io images ls` 核对 |
| worker 节点 OOM | 减副本/固定有状态组件到 master（见"进阶调度"）；观察 `free -h` 与 `kubectl top nodes` |
| HPA 显示 unknown | metrics-server 未就绪：`kubectl -n kube-system get pods` 检查，`kubectl top nodes` 验证 |

| 搜索无结果但首页有职位 | `kubectl -n zhitongche rollout restart deploy/backend`（启动时全量同步 ES） |
| join 命令过期 | master 上 `kubeadm token create --print-join-command` 重新生成 |

