import redis
from rq import Queue

# 连接到 Redis
redis_url = 'redis://localhost:6379'
conn = redis.from_url(redis_url)

# 获取默认队列
q = Queue(connection=conn)

# 清空队列
num_jobs_cleared = q.empty()

print(f"队列 '{q.name}' 已清空。共删除了 {num_jobs_cleared} 个任务。")

# 也可以清空失败任务队列和延迟任务队列（可选，但推荐）
try:
    from rq.registry import FailedJobRegistry, DeferredJobRegistry
    
    failed_registry = FailedJobRegistry(queue=q)
    deferred_registry = DeferredJobRegistry(queue=q)

    for job_id in failed_registry.get_job_ids():
        failed_registry.remove(job_id, delete_job=True)
    print("失败任务队列已清空。")

    for job_id in deferred_registry.get_job_ids():
        deferred_registry.remove(job_id, delete_job=True)
    print("延迟任务队列已清空。")

except ImportError:
    print("警告：无法导入任务注册表（Registry）。可能 rq 版本较旧。仅清空了主队列。")
except Exception as e:
    print(f"清空其他队列时出错: {e}")
