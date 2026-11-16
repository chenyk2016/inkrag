"""首次生产事实表；DDL在开发时冻结，后续变更必须新增迁移版本。"""

from alembic import op

revision = "0001_production"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """建立独立PG表/索引与最小运行角色权限，不读取或迁移旧资料。"""
    op.execute(
        "\nCREATE TABLE ip_api_keys (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\trevoked BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (digest)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_audit (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tsubject VARCHAR(256) NOT NULL, \n\taction VARCHAR(64) NOT NULL, \n\ttarget_id VARCHAR(128) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_builds (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\tjob_id VARCHAR(32) NOT NULL, \n\tindex_name VARCHAR(255) NOT NULL, \n\tmodel_revision VARCHAR(255) NOT NULL, \n\tkb_revision INTEGER NOT NULL, \n\tauthz_epoch INTEGER NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (index_name)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_evaluations (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\trelease_id VARCHAR(32) NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tmetrics JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_job_attempts (\n\tjob_id VARCHAR(32) NOT NULL, \n\tepoch INTEGER NOT NULL, \n\towner VARCHAR(128) NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (job_id, epoch)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_knowledge_bases (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tauthz_epoch INTEGER NOT NULL, \n\tactive_release_id VARCHAR(32), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_outbox (\n\tid VARCHAR(32) NOT NULL, \n\tjob_id VARCHAR(32) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_releases (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\tbuild_id VARCHAR(32) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tindex_name VARCHAR(255) NOT NULL, \n\tmodel_revision VARCHAR(255) NOT NULL, \n\tauthz_epoch INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, kb_id, version)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_samples (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\tquery VARCHAR(4000) NOT NULL, \n\texpected_document_ids JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_documents (\n\tid VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tsource_asset_id VARCHAR(64) NOT NULL, \n\tresult_id VARCHAR(64), \n\trevision INTEGER NOT NULL, \n\tindexed_result_id VARCHAR(64), \n\tattributes JSONB NOT NULL, \n\tdeleted BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id, kb_id) REFERENCES ip_knowledge_bases (tenant_id, id), \n\tUNIQUE (tenant_id, id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_jobs (\n\tid VARCHAR(32) NOT NULL, \n\toperation_id VARCHAR(32) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tkb_id VARCHAR(32) NOT NULL, \n\tdocument_id VARCHAR(32), \n\tkind VARCHAR(32) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tstage VARCHAR(32) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tresult JSONB, \n\texpected_result_id VARCHAR(64), \n\texpected_document_revision INTEGER, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tidempotency_key VARCHAR(256) NOT NULL, \n\tattempt INTEGER NOT NULL, \n\tlease_epoch INTEGER NOT NULL, \n\tlease_owner VARCHAR(128), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tnext_retry_at TIMESTAMP WITH TIME ZONE, \n\terror_code VARCHAR(128), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, kind, idempotency_key), \n\tFOREIGN KEY(tenant_id, kb_id) REFERENCES ip_knowledge_bases (tenant_id, id), \n\tUNIQUE (operation_id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_asset_references (\n\tdocument_id VARCHAR(32) NOT NULL, \n\tasset_id VARCHAR(64) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\tPRIMARY KEY (document_id, asset_id), \n\tFOREIGN KEY(tenant_id, document_id) REFERENCES ip_documents (tenant_id, id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE ip_result_versions (\n\tdocument_id VARCHAR(32) NOT NULL, \n\tresult_id VARCHAR(64) NOT NULL, \n\ttenant_id VARCHAR(128) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tprevious_result_id VARCHAR(64), \n\tasset_ids JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (document_id, result_id), \n\tFOREIGN KEY(tenant_id, document_id) REFERENCES ip_documents (tenant_id, id), \n\tUNIQUE (tenant_id, document_id, revision)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_ip_api_keys_kb_id ON ip_api_keys (kb_id)")
    op.execute("CREATE INDEX ix_ip_api_keys_tenant_id ON ip_api_keys (tenant_id)")
    op.execute("CREATE INDEX ix_ip_audit_tenant_id ON ip_audit (tenant_id)")
    op.execute("CREATE INDEX ix_ip_builds_job_id ON ip_builds (job_id)")
    op.execute("CREATE INDEX ix_ip_builds_kb_id ON ip_builds (kb_id)")
    op.execute("CREATE INDEX ix_ip_builds_tenant_id ON ip_builds (tenant_id)")
    op.execute("CREATE INDEX ix_ip_evaluations_kb_id ON ip_evaluations (kb_id)")
    op.execute("CREATE INDEX ix_ip_evaluations_tenant_id ON ip_evaluations (tenant_id)")
    op.execute("CREATE INDEX ix_ip_knowledge_bases_tenant_id ON ip_knowledge_bases (tenant_id)")
    op.execute("CREATE INDEX ix_ip_outbox_job_id ON ip_outbox (job_id)")
    op.execute("CREATE INDEX ix_ip_outbox_status ON ip_outbox (status)")
    op.execute("CREATE INDEX ix_ip_releases_kb_id ON ip_releases (kb_id)")
    op.execute("CREATE INDEX ix_ip_releases_tenant_id ON ip_releases (tenant_id)")
    op.execute("CREATE INDEX ix_ip_samples_kb_id ON ip_samples (kb_id)")
    op.execute("CREATE INDEX ix_ip_samples_tenant_id ON ip_samples (tenant_id)")
    op.execute("CREATE INDEX ix_ip_documents_kb_id ON ip_documents (kb_id)")
    op.execute("CREATE INDEX ix_ip_documents_tenant_id ON ip_documents (tenant_id)")
    op.execute("CREATE INDEX ix_ip_jobs_kb_id ON ip_jobs (kb_id)")
    op.execute("CREATE INDEX ix_ip_jobs_status ON ip_jobs (status)")
    op.execute("CREATE INDEX ix_ip_jobs_tenant_id ON ip_jobs (tenant_id)")
    op.execute("CREATE INDEX ix_ip_asset_references_tenant_id ON ip_asset_references (tenant_id)")
    op.execute("CREATE INDEX ix_ip_result_versions_tenant_id ON ip_result_versions (tenant_id)")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='inkrag_app') THEN GRANT USAGE ON SCHEMA public TO inkrag_app; GRANT SELECT,INSERT,UPDATE,DELETE ON ip_api_keys,ip_audit,ip_builds,ip_evaluations,ip_job_attempts,ip_knowledge_bases,ip_outbox,ip_releases,ip_samples,ip_documents,ip_jobs,ip_asset_references,ip_result_versions TO inkrag_app; GRANT SELECT ON ip_alembic_version TO inkrag_app; END IF; END $$"
    )


def downgrade() -> None:
    """仅显式回滚时删除本版本自建表，生产回滚前必须备份。"""
    op.drop_table("ip_result_versions")
    op.drop_table("ip_asset_references")
    op.drop_table("ip_jobs")
    op.drop_table("ip_documents")
    op.drop_table("ip_samples")
    op.drop_table("ip_releases")
    op.drop_table("ip_outbox")
    op.drop_table("ip_knowledge_bases")
    op.drop_table("ip_job_attempts")
    op.drop_table("ip_evaluations")
    op.drop_table("ip_builds")
    op.drop_table("ip_audit")
    op.drop_table("ip_api_keys")
