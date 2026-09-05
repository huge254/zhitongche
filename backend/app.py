"""
职通车·求职招聘平台 —— 后端服务
技术栈：Flask + MySQL（业务数据）+ Elasticsearch（职位全文检索）+ Redis（热门职位缓存）
部署形态：Kubernetes Deployment + Service + HPA
"""
import os
import time
import json
import logging

from flask import Flask, request, jsonify
import pymysql
from elasticsearch import Elasticsearch, helpers
import redis as redis_lib

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ztc-backend")

DB_HOST = os.getenv("DB_HOST", "mysql")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root123456")
DB_NAME = os.getenv("DB_NAME", "zhitongche")
ES_URL = os.getenv("ES_URL", "http://elasticsearch:9200")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
HOT_CACHE_KEY = "ztc:hot_jobs"

app = Flask(__name__)

# Redis 客户端（模块级初始化，不可用时降级为不缓存）
try:
    rds = redis_lib.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    rds.ping()
    log.info("Redis 连接就绪")
except Exception as e:
    log.warning("Redis 不可用（热门职位将不缓存）: %s", e)
    rds = None

JOB_INDEX = "job"
JOB_MAPPING = {
    "properties": {
        "jobId": {"type": "long"},
        "title": {"type": "text"},
        "city": {"type": "keyword"},
        "salaryMin": {"type": "integer"},
        "salaryMax": {"type": "integer"},
        "description": {"type": "text"},
        "requirement": {"type": "text"},
        "company": {"type": "keyword"},
        "status": {"type": "integer"},
        "createdAt": {"type": "keyword"},
    }
}


# ---------- 基础工具 ----------

def ok(data=None, message="ok"):
    return jsonify({"code": 200, "message": message, "data": data})


def err(message, code=400):
    return jsonify({"code": code, "message": message, "data": None})


def get_db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
    )


def job_to_dict(j):
    """MySQL 行 / ES 文档统一转成前端要的 camelCase 结构。"""
    return {
        "jobId": j.get("jobId") or j.get("id"),
        "title": j.get("title"),
        "city": j.get("city"),
        "salaryMin": j.get("salaryMin") or j.get("salary_min"),
        "salaryMax": j.get("salaryMax") or j.get("salary_max"),
        "description": j.get("description"),
        "requirement": j.get("requirement"),
        "company": j.get("company"),
        "status": j.get("status"),
        "createdAt": str(j.get("createdAt") or j.get("created_at") or ""),
    }


# ---------- 启动等待 ----------

def wait_mysql(retries=60, interval=3):
    for i in range(1, retries + 1):
        try:
            conn = get_db()
            conn.close()
            log.info("MySQL 连接就绪")
            return
        except Exception as e:
            log.warning("等待 MySQL 就绪（%d/%d）: %s", i, retries, e)
            time.sleep(interval)
    raise RuntimeError("MySQL 长时间未就绪")


def build_es():
    return Elasticsearch(ES_URL, request_timeout=10)


def wait_es(retries=60, interval=3):
    for i in range(1, retries + 1):
        try:
            if build_es().ping():
                log.info("Elasticsearch 连接就绪")
                return
        except Exception as e:
            log.warning("等待 Elasticsearch 就绪（%d/%d）: %s", i, retries, e)
        time.sleep(interval)
    raise RuntimeError("Elasticsearch 长时间未就绪")


# ---------- ES 索引管理 ----------

def ensure_es_index():
    es = build_es()
    if not es.indices.exists(index=JOB_INDEX):
        es.indices.create(index=JOB_INDEX, mappings=JOB_MAPPING)
        log.info("ES 索引 %s 已创建", JOB_INDEX)
    if es.count(index=JOB_INDEX)["count"] == 0:
        sync_jobs_to_es()


def sync_jobs_to_es():
    """把 MySQL 中所有招聘中的职位批量写入 ES（启动时兜底同步）。"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.*, c.name AS company FROM job j "
                "LEFT JOIN company c ON j.company_id = c.id"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    actions = []
    for j in rows:
        if j.get("status") != 1:
            continue
        actions.append({
            "_index": JOB_INDEX,
            "_id": j["id"],
            "_source": {
                "jobId": j["id"],
                "title": j["title"],
                "city": j["city"],
                "salaryMin": j["salary_min"],
                "salaryMax": j["salary_max"],
                "description": j["description"],
                "requirement": j["requirement"],
                "company": j.get("company"),
                "status": j["status"],
                "createdAt": str(j.get("created_at") or ""),
            },
        })
    if actions:
        helpers.bulk(build_es(), actions)
    log.info("已同步 %d 条职位到 ES", len(actions))


def index_job(job_id):
    """发布/变更单个职位时同步 ES。"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.*, c.name AS company FROM job j "
                "LEFT JOIN company c ON j.company_id = c.id WHERE j.id = %s",
                (job_id,),
            )
            j = cur.fetchone()
    finally:
        conn.close()
    if not j:
        return
    es = build_es()
    if j.get("status") == 1:
        es.index(index=JOB_INDEX, id=j["id"], document={
            "jobId": j["id"],
            "title": j["title"],
            "city": j["city"],
            "salaryMin": j["salary_min"],
            "salaryMax": j["salary_max"],
            "description": j["description"],
            "requirement": j["requirement"],
            "company": j.get("company"),
            "status": j["status"],
            "createdAt": str(j.get("created_at") or ""),
        })
    else:
        try:
            es.delete(index=JOB_INDEX, id=j["id"])
        except Exception:
            pass


# ---------- 健康检查与压测 ----------

@app.route("/api/health")
def health():
    return ok("zhitongche-backend running")


@app.route("/api/stress")
def stress():
    """HPA 演示用：持续占用 CPU 一段时间。"""
    seconds = min(request.args.get("seconds", 2, type=int) or 2, 10)
    end = time.time() + seconds
    x = 0
    while time.time() < end:
        x += sum(i * i for i in range(3000))
    return ok({"burned": seconds, "x": x % 1000})


# ---------- 职位检索 ----------

def es_search(keyword, city, salary_min, salary_max):
    must, filters = [], [{"term": {"status": 1}}]
    if keyword:
        must.append({"multi_match": {
            "query": keyword,
            "fields": ["title^3", "description", "requirement"],
        }})
    if city:
        filters.append({"term": {"city": city}})
    if salary_min is not None:
        filters.append({"range": {"salaryMax": {"gte": salary_min}}})
    if salary_max is not None:
        filters.append({"range": {"salaryMin": {"lte": salary_max}}})

    resp = build_es().search(
        index=JOB_INDEX,
        query={"bool": {"must": must, "filter": filters}},
        sort=[{"createdAt": "desc"}],
        size=50,
    )
    return [hit["_source"] for hit in resp["hits"]["hits"]]


def mysql_fallback_search(keyword, city, salary_min, salary_max):
    """ES 不可用时的降级查询（MySQL LIKE）。"""
    where = ["j.status = 1"]
    params = []
    if keyword:
        where.append("(j.title LIKE %s OR j.description LIKE %s OR j.requirement LIKE %s)")
        params += [f"%{keyword}%"] * 3
    if city:
        where.append("j.city = %s")
        params.append(city)
    if salary_min is not None:
        where.append("j.salary_max >= %s")
        params.append(salary_min)
    if salary_max is not None:
        where.append("j.salary_min <= %s")
        params.append(salary_max)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.*, c.name AS company FROM job j "
                "LEFT JOIN company c ON j.company_id = c.id "
                "WHERE " + " AND ".join(where) +
                " ORDER BY j.created_at DESC LIMIT 50",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [job_to_dict(r) for r in rows]


@app.route("/api/job/search")
def job_search():
    keyword = request.args.get("keyword", "").strip()
    city = request.args.get("city", "").strip()
    salary_min = request.args.get("salaryMin", type=int)
    salary_max = request.args.get("salaryMax", type=int)
    try:
        return ok(es_search(keyword, city, salary_min, salary_max))
    except Exception as e:
        log.warning("ES 查询失败，降级到 MySQL: %s", e)
        return ok(mysql_fallback_search(keyword, city, salary_min, salary_max))


@app.route("/api/job/hot")
def job_hot():
    """热门职位：按投递数排序，Redis 缓存 60 秒。"""
    try:
        cached = rds.get(HOT_CACHE_KEY)
        if cached:
            return ok(json.loads(cached))
    except Exception:
        pass

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.id, j.title, j.city, j.salary_min, j.salary_max, "
                "c.name AS company, COUNT(a.id) AS cnt "
                "FROM job j LEFT JOIN company c ON j.company_id = c.id "
                "LEFT JOIN application a ON a.job_id = j.id "
                "WHERE j.status = 1 "
                "GROUP BY j.id ORDER BY cnt DESC, j.id DESC LIMIT 5"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result = [{
        "jobId": r["id"], "title": r["title"], "city": r["city"],
        "salaryMin": r["salary_min"], "salaryMax": r["salary_max"],
        "company": r["company"], "applyCount": r["cnt"],
    } for r in rows]

    try:
        rds.setex(HOT_CACHE_KEY, 60, json.dumps(result, ensure_ascii=False))
    except Exception:
        pass
    return ok(result)


@app.route("/api/job/<int:job_id>")
def job_detail(job_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.*, c.name AS company FROM job j "
                "LEFT JOIN company c ON j.company_id = c.id WHERE j.id = %s",
                (job_id,),
            )
            j = cur.fetchone()
    finally:
        conn.close()
    if not j:
        return err("职位不存在", 404)
    return ok(job_to_dict(j))


# ---------- 企业端 ----------

@app.route("/api/company/register", methods=["POST"])
def company_register():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    contact = (body.get("contact") or "").strip()
    if not name:
        return err("企业名称不能为空")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO company (name, contact, status) VALUES (%s, %s, 0)", (name, contact))
            cid = cur.lastrowid
    finally:
        conn.close()
    return ok({"companyId": cid, "status": 0})


@app.route("/api/company/mine")
def company_mine():
    cid = request.args.get("companyId", type=int)
    if not cid:
        return err("缺少 companyId")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM company WHERE id = %s", (cid,))
            c = cur.fetchone()
    finally:
        conn.close()
    if not c:
        return err("企业不存在", 404)
    return ok({"companyId": c["id"], "name": c["name"], "contact": c["contact"], "status": c["status"]})


@app.route("/api/job/publish", methods=["POST"])
def job_publish():
    body = request.get_json(silent=True) or {}
    company_id = body.get("companyId")
    title = (body.get("title") or "").strip()
    if not company_id or not title:
        return err("参数不完整")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM company WHERE id = %s", (company_id,))
            c = cur.fetchone()
            if not c:
                return err("企业不存在", 404)
            if c["status"] != 1:
                return err("企业审核通过后可发布职位")
            cur.execute(
                "INSERT INTO job (company_id, title, city, salary_min, salary_max, description, requirement, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1)",
                (
                    company_id, title,
                    (body.get("city") or "").strip(),
                    body.get("salaryMin") or 0,
                    body.get("salaryMax") or 0,
                    body.get("description") or "",
                    body.get("requirement") or "",
                ),
            )
            job_id = cur.lastrowid
    finally:
        conn.close()

    try:
        index_job(job_id)
    except Exception as e:
        log.warning("新职位写入 ES 失败（不影响发布）: %s", e)
    return ok({"jobId": job_id})


# ---------- 学生端 ----------

@app.route("/api/resume/save", methods=["POST"])
def resume_save():
    body = request.get_json(silent=True) or {}
    name = (body.get("studentName") or "").strip()
    if not name:
        return err("姓名不能为空")
    rid = body.get("resumeId")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            if rid:
                cur.execute(
                    "UPDATE resume SET student_name=%s, school=%s, major=%s, skills=%s, self_intro=%s WHERE id=%s",
                    (name, body.get("school"), body.get("major"), body.get("skills"), body.get("selfIntro"), rid),
                )
            else:
                cur.execute(
                    "INSERT INTO resume (student_name, school, major, skills, self_intro) VALUES (%s,%s,%s,%s,%s)",
                    (name, body.get("school"), body.get("major"), body.get("skills"), body.get("selfIntro")),
                )
                rid = cur.lastrowid
    finally:
        conn.close()
    return ok({"resumeId": rid})


@app.route("/api/resume/mine")
def resume_mine():
    rid = request.args.get("resumeId", type=int)
    if not rid:
        return err("缺少 resumeId")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM resume WHERE id = %s", (rid,))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return err("简历不存在", 404)
    return ok({
        "resumeId": r["id"], "studentName": r["student_name"], "school": r["school"],
        "major": r["major"], "skills": r["skills"], "selfIntro": r["self_intro"],
    })


@app.route("/api/application/submit", methods=["POST"])
def application_submit():
    body = request.get_json(silent=True) or {}
    job_id = body.get("jobId")
    resume_id = body.get("resumeId")
    if not job_id or not resume_id:
        return err("参数不完整")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM job WHERE id = %s", (job_id,))
            j = cur.fetchone()
            if not j:
                return err("职位不存在", 404)
            if j["status"] != 1:
                return err("该职位已关闭")
            try:
                cur.execute(
                    "INSERT INTO application (job_id, resume_id, status) VALUES (%s, %s, 0)",
                    (job_id, resume_id),
                )
            except pymysql.err.IntegrityError:
                return err("请勿重复投递")
            aid = cur.lastrowid
    finally:
        conn.close()

    try:
        rds.delete(HOT_CACHE_KEY)  # 投递数变化，失效热门缓存
    except Exception:
        pass
    return ok({"applicationId": aid})


# ---------- 管理端 ----------

@app.route("/api/admin/company/list")
def admin_company_list():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM company ORDER BY created_at DESC LIMIT 200")
            rows = cur.fetchall()
    finally:
        conn.close()
    return ok([{"companyId": c["id"], "name": c["name"], "contact": c["contact"],
                "status": c["status"], "createdAt": str(c.get("created_at") or "")} for c in rows])


@app.route("/api/admin/company/audit", methods=["POST"])
def admin_company_audit():
    body = request.get_json(silent=True) or {}
    cid = body.get("companyId")
    passed = bool(body.get("pass"))
    if not cid:
        return err("缺少 companyId")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE company SET status = %s WHERE id = %s", (1 if passed else 2, cid))
    finally:
        conn.close()
    return ok()


@app.route("/api/admin/application/list")
def admin_application_list():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.*, j.title AS job_title, r.student_name, c.name AS company "
                "FROM application a "
                "LEFT JOIN job j ON a.job_id = j.id "
                "LEFT JOIN resume r ON a.resume_id = r.id "
                "LEFT JOIN company c ON j.company_id = c.id "
                "ORDER BY a.apply_time DESC LIMIT 200"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return ok([{
        "applicationId": a["id"], "studentName": a.get("student_name") or "-",
        "jobTitle": a.get("job_title") or "-", "company": a.get("company") or "-",
        "status": a["status"], "applyTime": str(a.get("apply_time") or ""),
    } for a in rows])


@app.route("/api/admin/application/status", methods=["POST"])
def admin_application_status():
    body = request.get_json(silent=True) or {}
    aid = body.get("applicationId")
    status = body.get("status")
    if not aid or status is None or int(status) < 0 or int(status) > 4:
        return err("参数非法")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE application SET status = %s WHERE id = %s", (int(status), aid))
    finally:
        conn.close()
    return ok()


if __name__ == "__main__":
    wait_mysql()
    wait_es()
    ensure_es_index()
    log.info("职通车后端启动完成，监听 8000")
    app.run(host="0.0.0.0", port=8000)
