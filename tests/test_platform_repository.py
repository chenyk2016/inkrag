"""显式启用真实PG验证；不以SQLite或内存事务代替生产仓库语义。"""

import os
import subprocess
import sys
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import override
from uuid import uuid4

import pytest
from inkrag_platform import workspace
from inkrag_platform.database import JobRow, KnowledgeBaseRow
from inkrag_platform.models import DomainError, MetadataField, Principal
from inkrag_platform.repository import Repository
from inkrag_platform.workspace_models import SelectionRequest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("INKRAG_TEST_DATABASE_DSN"), reason="需要显式专用PG测试DSN和已运行迁移"
    ),
]


@pytest.fixture
def repo() -> Generator[Repository, None, None]:
    """仅使用调用方显式提供的测试PG，关闭连接不删除数据库。"""
    value = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])
    value.check()
    yield value
    value.close()


@pytest.fixture
def actor() -> Principal:
    """每项测试独立租户，避免覆盖开发资料或并行测试数据。"""
    return Principal(tenant_id="test-" + uuid4().hex, subject="test-admin", roles=["admin"])


def parsed(repo: Repository, actor: Principal) -> tuple[str, str, str]:
    """仅构造PG事实夹具；S3及实际解析由独立贯穿集成验证。"""
    kb = repo.create_kb(actor, "仓库测试")
    job = repo.submit_parse(actor, kb.kb_id, "manual.md", "a" * 64, "first")
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    doc = repo.complete_parse(
        job.job_id, "fixture", lease.lease_epoch, "b" * 64, 1, None, ["c" * 64]
    )
    return kb.kb_id, doc.document_id, job.job_id


def test_pg_atomic_accept_scope_and_idempotency(repo: Repository, actor: Principal) -> None:
    """接受结果原子包含资料/job/outbox，跨租户及异请求重用幂等键明确拒绝。"""
    kb = repo.create_kb(actor, "事务KB")
    a = repo.submit_parse(actor, kb.kb_id, "a.md", "a" * 64, "request-key")
    b = repo.submit_parse(actor, kb.kb_id, "a.md", "a" * 64, "request-key")
    assert a.job_id == b.job_id and a.document_id == b.document_id
    assert len(repo.list_documents(actor, kb.kb_id)) == 1
    with repo.transaction() as session:
        count = session.scalar(
            text("SELECT count(*) FROM ip_outbox WHERE job_id=:id"), {"id": a.job_id}
        )
        assert count == 1
    with pytest.raises(DomainError) as conflict:
        repo.submit_parse(actor, kb.kb_id, "other.md", "d" * 64, "request-key")
    assert conflict.value.code == "idempotency_conflict"
    assert a.document_id
    alien = Principal(tenant_id="alien-" + uuid4().hex, subject="other", roles=["admin"])
    with pytest.raises(DomainError) as denied:
        repo.get_document(alien, a.document_id)
    assert denied.value.code == "not_found"
    viewer = Principal(tenant_id=actor.tenant_id, subject="reader", roles=["viewer"])
    with pytest.raises(DomainError) as denied_write:
        repo.submit_parse(viewer, kb.kb_id, "x.txt", "f" * 64, "reader")
    assert denied_write.value.code == "forbidden"


def test_two_instances_conflict_and_lease_fence(repo: Repository, actor: Principal) -> None:
    """两连接池相同expected版本只能提交一个；过期Worker不能完成旧attempt。"""
    kb_id, document_id, _ = parsed(repo, actor)
    second = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])

    def save(item: tuple[Repository, str]) -> str:
        """模拟两个API副本从相同解析版本并发保存。"""
        target, identity = item
        try:
            target.save_revision(
                actor, document_id, "b" * 64, identity * 64, 2, "b" * 64, ["c" * 64]
            )
            return "ok"
        except DomainError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [(repo, "d"), (second, "e")]))
        assert sorted(results) == ["ok", "version_conflict"]
    finally:
        second.close()
    current = repo.get_document(actor, document_id)
    job = repo.accept_job(actor, "ingest", kb_id, document_id, {}, "lease-check", current.result_id)
    first = repo.claim_job(job.job_id, "old-worker", 5)
    assert first
    with repo.transaction() as session:
        row = session.scalar(select(JobRow).where(JobRow.id == job.job_id).with_for_update())
        assert row
        session.execute(
            text(
                "UPDATE ip_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
            ),
            {"id": job.job_id},
        )
    newer = repo.claim_job(job.job_id, "new-worker", 30)
    assert newer and newer.lease_epoch > first.lease_epoch
    with pytest.raises(DomainError) as stale:
        repo.finish_job(job.job_id, "old-worker", first.lease_epoch, {})
    assert stale.value.code == "lease_lost"
    done = repo.finish_job(job.job_id, "new-worker", newer.lease_epoch, {"checked": True})
    assert done.status == "succeeded"
    assert repo.claim_job(job.job_id, "duplicate") is None


def test_pg_publish_delete_rollback_and_keys(repo: Repository, actor: Principal) -> None:
    """发布事实由PG切换，删除立即使旧发布和资产不可访问；凭证撤销生效。"""
    kb_id, document_id, _ = parsed(repo, actor)
    job = repo.accept_job(actor, "ingest", kb_id, document_id, {}, "build", "b" * 64)
    claimed = repo.claim_job(job.job_id, "builder")
    assert claimed
    build = repo.start_build(
        job.job_id, "builder", claimed.lease_epoch, "test-index-" + uuid4().hex, "model-sha"
    )
    assert build.manifest[0].result_id == "b" * 64
    # 此处只验证PG规则，实际OpenSearch冻结由搜索贯穿测试证明。
    repo.complete_build(job.job_id, "builder", claimed.lease_epoch, build.build_id)
    release = repo.publish_build(actor, build.build_id)
    assert repo.get_active_release(actor, kb_id).release_id == release.release_id
    key = repo.create_api_key(actor, kb_id, "read-only")
    caller = repo.authenticate_api_key(key.token)
    assert caller and caller.kb_ids == [kb_id]
    assert repo.get_active_release(caller, kb_id).release_id == release.release_id
    assert repo.has_asset(caller, "a" * 64)
    with pytest.raises(DomainError):
        repo.delete_document(caller, document_id, 1)
    repo.revoke_api_key(actor, key.key_id)
    assert repo.authenticate_api_key(key.token) is None
    repo.delete_document(actor, document_id, 1)
    assert not repo.has_asset(actor, "a" * 64)
    with pytest.raises(DomainError) as revoked:
        repo.get_active_release(actor, kb_id)
    assert revoked.value.code == "authorization_pending"
    with pytest.raises(DomainError):
        repo.rollback_release(actor, release.release_id, release.release_id)


def test_outbox_recovery_and_cancel(repo: Repository, actor: Principal) -> None:
    """失败投递可重领；取消持久化fence使已领取任务不能再发布。"""
    kb = repo.create_kb(actor, "任务KB")
    job = repo.submit_parse(actor, kb.kb_id, "a.txt", "a" * 64, "cancel")
    events = repo.claim_outbox(100, job_id=job.job_id)
    own = [e for e in events if e.job_id == job.job_id]
    assert own
    repo.mark_outbox(own[0].event_id, own[0].attempts, False)
    retry = [e for e in repo.claim_outbox(100, job_id=job.job_id) if e.job_id == job.job_id]
    assert retry and retry[0].attempts == own[0].attempts + 1
    repo.mark_outbox(retry[0].event_id, retry[0].attempts, True)
    claimed = repo.claim_job(job.job_id, "worker")
    assert claimed
    assert repo.cancel_job(actor, job.job_id).status == "cancelled"
    with pytest.raises(DomainError) as fenced:
        repo.complete_parse(job.job_id, "worker", claimed.lease_epoch, "b" * 64, 1, None, [])
    assert fenced.value.code == "lease_lost"


def test_retry_wait_recovers_confirmed_but_unclaimed_delivery(
    repo: Repository, actor: Principal
) -> None:
    """已确认的重试消息未被 Worker 领取时，PG 在宽限期后生成新的投递意图。"""
    kb = repo.create_kb(actor, "重试消息恢复")
    job = repo.submit_parse(actor, kb.kb_id, "retry.txt", "a" * 64, "retry-recovery")
    (first_event,) = repo.claim_outbox(job_id=job.job_id)
    repo.mark_outbox(first_event.event_id, first_event.attempts, True)
    claimed = repo.claim_job(job.job_id, "first-worker")
    assert claimed
    failed = repo.fail_job(
        job.job_id, "first-worker", claimed.lease_epoch, "storage_busy", retry=True
    )
    assert failed.status == "retry_wait"

    with repo.transaction() as session:
        session.execute(
            text(
                "UPDATE ip_jobs SET next_retry_at=clock_timestamp()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": job.job_id},
        )
    (retry_event,) = repo.claim_outbox(job_id=job.job_id)
    repo.mark_outbox(retry_event.event_id, retry_event.attempts, True)
    assert repo.recover_jobs(job_id=job.job_id) == 0

    with repo.transaction() as session:
        session.execute(
            text(
                "UPDATE ip_jobs SET updated_at=clock_timestamp()-interval '65 seconds', "
                "next_retry_at=clock_timestamp()-interval '61 seconds' WHERE id=:id"
            ),
            {"id": job.job_id},
        )
    assert repo.recover_jobs(job_id=job.job_id) == 1
    assert repo.recover_jobs(job_id=job.job_id) == 0
    (replacement_event,) = repo.claim_outbox(job_id=job.job_id)
    assert replacement_event.event_id != retry_event.event_id
    repo.mark_outbox(replacement_event.event_id, replacement_event.attempts, True)
    recovered = repo.claim_job(job.job_id, "replacement")
    assert recovered and recovered.lease_epoch > claimed.lease_epoch and recovered.attempt == 2
    done = repo.finish_job(job.job_id, "replacement", recovered.lease_epoch, {"recovered": True})
    assert done.status == "succeeded"


def test_metadata_schema_cas_and_parse_revision_are_independent(
    repo: Repository, actor: Principal
) -> None:
    """属性CAS推进资料版本后仍可修订解析链；日期和枚举保留真实声明类型。"""
    kb_id, document_id, _ = parsed(repo, actor)
    fields = [
        MetadataField(name="published", label="发布日期", type="date"),
        MetadataField(name="team", label="部门", type="enum", options=["研发", "产品"]),
        MetadataField(name="public", label="公开", type="boolean"),
    ]
    kb = repo.get_kb(actor, kb_id)
    kb = repo.set_fields(actor, kb_id, kb.revision, fields)
    changed = repo.update_metadata(
        actor, document_id, 1, {"published": "2026-11-09", "team": "研发", "public": False}
    )
    assert changed.revision == 2 and changed.result_id == "b" * 64
    with pytest.raises(DomainError) as invalid:
        repo.update_metadata(actor, document_id, 2, {"published": "yesterday"})
    assert invalid.value.code == "invalid_metadata"
    with pytest.raises(DomainError):
        repo.update_metadata(actor, document_id, 1, {})
    with pytest.raises(DomainError):
        repo.set_fields(actor, kb_id, repo.get_kb(actor, kb_id).revision, [])
    revised = repo.save_revision(actor, document_id, "b" * 64, "d" * 64, 2, "b" * 64, [])
    assert revised.revision == 3
    assert repo.get_version(actor, document_id).revision == 2
    job = repo.accept_job(actor, "ingest", kb_id, document_id, {}, "metadata-build", "d" * 64)
    lease = repo.claim_job(job.job_id, "builder")
    assert lease
    build = repo.start_build(job.job_id, "builder", lease.lease_epoch, "test-" + uuid4().hex, "m")
    assert build.field_schema == {"published": "date", "team": "text", "public": "boolean"}
    assert build.manifest[0].revision == 3
    repo.complete_build(job.job_id, "builder", lease.lease_epoch, build.build_id)
    assert repo.get_document(actor, document_id).indexed_revision is None
    repo.publish_build(actor, build.build_id)
    assert repo.get_document(actor, document_id).indexed_revision == 3


def test_evaluation_cancel_fence_and_tenant_disable(repo: Repository, actor: Principal) -> None:
    """取消评测不能留下成功记录；租户停用连资源摘要及服务凭证一起失效。"""
    kb_id, document_id, _ = parsed(repo, actor)
    job = repo.accept_job(actor, "build", kb_id, None, {}, "release")
    lease = repo.claim_job(job.job_id, "builder")
    assert lease
    build = repo.start_build(job.job_id, "builder", lease.lease_epoch, "test-" + uuid4().hex, "m")
    repo.complete_build(job.job_id, "builder", lease.lease_epoch, build.build_id)
    release = repo.publish_build(actor, build.build_id)
    evaluation = repo.accept_job(actor, "evaluate", kb_id, None, {}, "eval")
    lease = repo.claim_job(evaluation.job_id, "evaluator")
    assert lease
    repo.cancel_job(actor, evaluation.job_id)
    with pytest.raises(DomainError) as fenced:
        repo.record_job_evaluation(
            evaluation.job_id, "evaluator", lease.lease_epoch, release.release_id, "n", {"mrr": 1.0}
        )
    assert fenced.value.code == "lease_lost" and repo.list_evaluations(actor, kb_id) == []
    key = repo.create_api_key(actor, kb_id, "disabled-test")
    with repo.transaction() as session:
        session.execute(
            text("UPDATE ip_tenants SET active=false WHERE id=:id"), {"id": actor.tenant_id}
        )
    assert repo.list_kbs(actor) == [] and not repo.has_asset(actor, "a" * 64)
    assert repo.authenticate_api_key(key.token) is None
    with pytest.raises(DomainError):
        repo.get_document(actor, document_id)


def test_killed_process_recovers_new_epoch(repo: Repository, actor: Principal) -> None:
    """真实持租进程被杀后按数据库时钟回收；旧attempt不能在新执行者之后提交。"""
    kb = repo.create_kb(actor, "Worker故障")
    job = repo.accept_job(actor, "build", kb.kb_id, None, {}, "killed-worker")
    script = '''"""仅用于故障注入的独立持租进程。"""
import os
import sys
import time
from inkrag_platform.repository import Repository
r = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])
job = r.claim_job(sys.argv[1], "killed-worker", 5)
print(job.lease_epoch if job else -1, flush=True)
time.sleep(60)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script, job.job_id], stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        epoch = int(process.stdout.readline().strip())
        assert epoch > 0
        process.kill()
        process.wait(timeout=5)
        assert repo.claim_job(job.job_id, "too-early") is None
        time.sleep(5.1)
        assert repo.recover_jobs(job_id=job.job_id) == 1
        lease = repo.claim_job(job.job_id, "replacement", 30)
        assert lease and lease.lease_epoch > epoch
        with pytest.raises(DomainError) as stale:
            repo.finish_job(job.job_id, "killed-worker", epoch, {})
        assert stale.value.code == "lease_lost"
        repo.finish_job(job.job_id, "replacement", lease.lease_epoch, {"recovered": True})
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def test_cancel_between_peek_and_commit_cannot_reuse_orm_identity(
    repo: Repository, actor: Principal
) -> None:
    """精确复现peek缓存旧job后取消；锁后读取必须重载，不能覆盖取消为成功。"""
    reached, resume = Event(), Event()

    class PausedRepository(Repository):
        """仅在worker首个KB锁之后暂停，制造可重复的取消窗口。"""

        @override
        def _scope(
            self,
            session: Session,
            principal: Principal,
            kb_id: str,
            *,
            write: bool = False,
            lock: bool = False,
        ) -> KnowledgeBaseRow:
            """保留真实SQL，只插入一次测试同步屏障。"""
            row = super()._scope(session, principal, kb_id, write=write, lock=lock)
            if principal.subject == "worker" and lock and not reached.is_set():
                reached.set()
                if not resume.wait(5):
                    raise RuntimeError("测试同步超时")
            return row

    kb = repo.create_kb(actor, "取消竞态")
    job = repo.submit_parse(actor, kb.kb_id, "race.txt", "a" * 64, "race")
    claimed = repo.claim_job(job.job_id, "worker")
    assert claimed
    paused = PausedRepository(os.environ["INKRAG_TEST_DATABASE_DSN"])

    def complete() -> str:
        """旧执行者已读到running对象，随后遇到真实PG取消。"""
        try:
            paused.complete_parse(job.job_id, "worker", claimed.lease_epoch, "b" * 64, 1, None, [])
            return "unexpected_commit"
        except DomainError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(complete)
            try:
                assert reached.wait(5)
                repo.cancel_job(actor, job.job_id)
            finally:
                resume.set()
            assert future.result(timeout=10) == "lease_lost"
        assert repo.get_job(actor, job.job_id).status == "cancelled"
        assert job.document_id
        assert repo.get_document(actor, job.document_id).result_id is None
    finally:
        resume.set()
        paused.close()


def test_build_budget_rejects_before_creating_manifest(repo: Repository, actor: Principal) -> None:
    """数量和字节超限在PG聚合后拒绝，不创建候选；列表只返回摘要且详情再校验预算。"""
    kb = repo.create_kb(actor, "预算反例")
    kb = repo.set_fields(
        actor, kb.kb_id, kb.revision, [MetadataField(name="description", label="描述", type="text")]
    )
    for index in range(2):
        job = repo.submit_parse(
            actor,
            kb.kb_id,
            f"budget-{index}.txt",
            "a" * 64,
            str(index),
            {"description": "文" * 400},
        )
        lease = repo.claim_job(job.job_id, "parser")
        assert lease
        doc = repo.complete_parse(job.job_id, "parser", lease.lease_epoch, "b" * 64, 1, None, [])
        workspace.select_result(
            repo,
            actor,
            doc.document_id,
            SelectionRequest(expected_document_revision=doc.revision, result_id=doc.result_id),
        )
    job = repo.accept_job(actor, "build", kb.kb_id, None, {}, "budget")
    lease = repo.claim_job(job.job_id, "builder")
    assert lease
    for documents, byte_limit in ((1, 8192), (2, 1024)):
        with pytest.raises(DomainError) as rejected:
            repo.start_build(
                job.job_id,
                "builder",
                lease.lease_epoch,
                "budget-" + uuid4().hex,
                "m",
                max_documents=documents,
                max_manifest_bytes=byte_limit,
            )
        assert rejected.value.code == "build_budget_exceeded"
        assert repo.list_builds(actor, kb.kb_id) == []
    build = repo.start_build(
        job.job_id,
        "builder",
        lease.lease_epoch,
        "budget-" + uuid4().hex,
        "m",
        max_documents=2,
        max_manifest_bytes=8192,
    )
    summary = repo.list_builds(actor, kb.kb_id)[0]
    assert summary.manifest == [] and not summary.manifest_included
    assert summary.document_count == 2 and 1024 < summary.manifest_bytes < 8192
    with pytest.raises(DomainError) as oversized_old:
        repo.get_build(actor, build.build_id, max_manifest_bytes=1024)
    assert oversized_old.value.code == "build_budget_exceeded"
    assert len(repo.get_build(actor, build.build_id).manifest) == 2
    repo.fail_job(job.job_id, "builder", lease.lease_epoch, "test_complete")


def test_publish_rejects_oversized_ready_build_and_keeps_active_release(
    repo: Repository, actor: Principal
) -> None:
    """已存在的超限候选不能覆盖线上，完整重建可绕过坏发布恢复。"""
    kb = repo.create_kb(actor, "发布预算恢复")

    def ready(key: str, *, full_rebuild: bool = False) -> str:
        """用正常任务路径冻结空库清单，测试只针对 PG 发布边界。"""
        job = repo.accept_job(actor, "build", kb.kb_id, None, {"full_rebuild": full_rebuild}, key)
        lease = repo.claim_job(job.job_id, "builder")
        assert lease
        build = repo.start_build(
            job.job_id, "builder", lease.lease_epoch, "budget-" + uuid4().hex, "model"
        )
        repo.complete_build(job.job_id, "builder", lease.lease_epoch, build.build_id)
        return build.build_id

    first = repo.publish_build(actor, ready("first-release"))
    oversized = ready("historic-candidate")
    with repo.transaction() as session:
        session.execute(
            text(
                "UPDATE ip_builds SET manifest = (SELECT jsonb_agg(jsonb_build_object("
                "'document_id',md5(i::text),'result_id',repeat('a',64),'revision',1,'metadata','{}'::jsonb)) "
                "FROM generate_series(1,5001) AS i) WHERE id=:id"
            ),
            {"id": oversized},
        )
    with pytest.raises(DomainError) as rejected:
        repo.publish_build(actor, oversized, first.release_id)
    assert rejected.value.code == "build_budget_exceeded"
    assert repo.get_active_release(actor, kb.kb_id).release_id == first.release_id

    # 模拟早期版本已经发布、后来因查询预算无法读取的清单，覆盖直接恢复路径。
    with repo.transaction() as session:
        session.execute(
            text("UPDATE ip_releases SET build_id=:build_id WHERE id=:release_id"),
            {"build_id": oversized, "release_id": first.release_id},
        )
    recovered = repo.publish_build(
        actor, ready("clean-recovery", full_rebuild=True), first.release_id
    )
    assert recovered.release_id != first.release_id
    with pytest.raises(DomainError) as rollback:
        repo.rollback_release(actor, first.release_id, recovered.release_id)
    assert rollback.value.code == "build_budget_exceeded"
    assert repo.get_active_release(actor, kb.kb_id).release_id == recovered.release_id


def test_pg_business_lock_excludes_writes_but_allows_fk_audit(
    repo: Repository, actor: Principal
) -> None:
    """实际SQL使用NO KEY UPDATE；业务写锁互斥，但持锁时取消审计FK可以提交。"""
    from sqlalchemy import event
    from sqlalchemy.engine import Connection, ExecutionContext

    kb = repo.create_kb(actor, "业务锁与审计FK")
    statements: list[str] = []

    def capture(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        """记录真实执行SQL，不用另写一份模拟查询代替实现验证。"""
        statements.append(statement)

    def audit() -> str:
        """另一事务只需审计FK的KEY SHARE，应在业务锁持有期间完成。"""
        with repo.transaction() as session:
            session.execute(text("SET LOCAL lock_timeout='1s'"))
            repo.audit_in_transaction(session, actor, "test.fk_audit", kb.kb_id, kb_id=kb.kb_id)
        return "audit_committed"

    def competing_business_lock() -> str:
        """另一业务写锁必须被阻止，不能把锁兼容修复成允许同时写KB。"""
        try:
            with repo.transaction() as session:
                session.execute(text("SET LOCAL lock_timeout='300ms'"))
                repo.scope_in_transaction(session, actor, kb.kb_id, write=True, lock=True)
            return "unexpected_acquire"
        except DomainError as error:
            return error.code

    event.listen(repo.engine, "before_cursor_execute", capture)
    try:
        with repo.transaction() as session:
            repo.scope_in_transaction(session, actor, kb.kb_id, write=True, lock=True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(audit).result(timeout=3) == "audit_committed"
                assert (
                    pool.submit(competing_business_lock).result(timeout=3) == "database_unavailable"
                )
        assert any(
            "FOR NO KEY UPDATE OF ip_knowledge_bases" in statement for statement in statements
        )
        assert not any("FOR UPDATE OF ip_knowledge_bases" in statement for statement in statements)
        with repo.transaction() as session:
            repo.scope_in_transaction(session, actor, kb.kb_id, write=True, lock=True)
    finally:
        event.remove(repo.engine, "before_cursor_execute", capture)


def test_required_field_backfill_preserves_selected_snapshot(
    repo: Repository, actor: Principal
) -> None:
    """现有资料先补字段再设必填，旧选用快照仍可保留，新导入不能省略。"""
    kb_id, document_id, _ = parsed(repo, actor)
    current = repo.get_document(actor, document_id)
    assert current.result_id is not None
    workspace.select_result(
        repo,
        actor,
        document_id,
        SelectionRequest(expected_document_revision=current.revision, result_id=current.result_id),
    )
    field = MetadataField(name="team", label="2", type="text", required=True)
    kb = repo.get_kb(actor, kb_id)
    with pytest.raises(DomainError) as missing:
        repo.set_fields(actor, kb_id, kb.revision, [field])
    assert missing.value.code == "required_fields_need_backfill"
    assert missing.value.details == {"missing_required_fields": {"team": 1}}
    assert repo.get_kb(actor, kb_id).revision == kb.revision
    repo.set_fields(actor, kb_id, kb.revision, [field.model_copy(update={"required": False})])
    current = repo.get_document(actor, document_id)
    updated = repo.update_metadata(actor, document_id, current.revision, {"team": "研发"})
    assert updated.selected_metadata == {}
    kb = repo.get_kb(actor, kb_id)
    saved = repo.set_fields(actor, kb_id, kb.revision, [field])
    assert saved.fields[0].required is True
    assert repo.get_document(actor, document_id).selected_metadata == {}
    with pytest.raises(DomainError) as rejected:
        repo.update_metadata(actor, document_id, updated.revision, {"team": "  "})
    assert rejected.value.code == "invalid_metadata"
    assert repo.get_document(actor, document_id).metadata == {"team": "研发"}


def test_business_fields_cannot_stale_an_active_parse(repo: Repository, actor: Principal) -> None:
    """解析任务占用资料版本时拒绝并发字段写入，解析仍能完成。"""
    kb = repo.create_kb(actor, "解析期间修改字段")
    field = MetadataField(name="team", label="部门", type="text")
    repo.set_fields(actor, kb.kb_id, kb.revision, [field])
    job = repo.submit_parse(actor, kb.kb_id, "manual.md", "a" * 64, "parse-before-edit")
    assert job.document_id
    current = repo.get_document(actor, job.document_id)
    with pytest.raises(DomainError) as pending:
        repo.update_metadata(actor, current.document_id, current.revision, {"team": "研发"})
    assert pending.value.code == "not_ready"
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    with pytest.raises(DomainError) as running:
        repo.update_metadata(actor, current.document_id, current.revision, {"team": "研发"})
    assert running.value.code == "not_ready"
    parsed_doc = repo.complete_parse(
        job.job_id, "fixture", lease.lease_epoch, "b" * 64, 1, None, []
    )
    edited = repo.update_metadata(
        actor, parsed_doc.document_id, parsed_doc.revision, {"team": "研发"}
    )
    assert edited.metadata == {"team": "研发"}
    assert edited.result_id == parsed_doc.result_id
