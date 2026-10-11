-- =============================================================================
-- app 库四表 DDL（W4 设计 §2.2 原文）
-- 执行方式：spring.sql.init.mode=always + classpath:schema-app.sql（幂等，全部 IF NOT EXISTS）
-- MySQL 8.0 中 `user` 是非保留关键字可直接建表（mysql.user 是系统库表，与本库无冲突）
-- =============================================================================

USE app;

-- 1. 用户表
CREATE TABLE IF NOT EXISTS `user` (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户 ID',
    username      VARCHAR(32)     NOT NULL COMMENT '用户名（唯一登录标识，[A-Za-z0-9_]{4,32}）',
    password_hash VARCHAR(100)    NOT NULL COMMENT 'BCrypt 哈希（strength 10 产出 60 字符，留余量）',
    hitl_enabled  TINYINT         NOT NULL DEFAULT 0 COMMENT 'SQL 执行前人工确认开关（W5 §3.8：0 关 / 1 开）',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户表';
-- 已存在库的补列走 deploy/w5_migrate_user_settings.sh 守护迁移（CREATE IF NOT EXISTS 不补列，
-- MySQL 8.0 无 ADD COLUMN IF NOT EXISTS，W5 设计 §3.8 / §8-⑮）。

-- 2. BYOK 密钥表（一人一行；列设计依据 §1.3/§1.4/§1.5）
CREATE TABLE IF NOT EXISTS api_key (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    user_id          BIGINT UNSIGNED NOT NULL COMMENT '所属用户 → user.id',
    key_ciphertext   VARBINARY(255)  NOT NULL COMMENT 'AES-256-GCM 密文（含 16 字节认证标签）',
    key_iv           VARBINARY(12)   NOT NULL COMMENT 'GCM 随机 IV，每次加密重新生成，严禁复用（§1.3）',
    key_fingerprint  CHAR(8)         NOT NULL COMMENT 'SHA-256(明文) 前 8 hex：日志替身与滥用反查（§1.5）',
    key_masked       VARCHAR(16)     NOT NULL COMMENT '掩码展示，如 sk-****ab3d；接口只返回此列',
    key_version      TINYINT         NOT NULL DEFAULT 1 COMMENT '主密钥版本号（预留轮换）',
    last_verified_at DATETIME        NULL COMMENT '最近一次通过 DeepSeek /models 校验时间',
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_api_key_user (user_id) COMMENT 'V1 一人一把，重复录入走更新',
    KEY idx_api_key_fingerprint (key_fingerprint) COMMENT '按指纹反查用户（滥用排查）'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户 BYOK 密钥（密文）';

-- 3. 查询历史表（Java 代理流结束时写入，§2.5；generated_sql 为 W5 few-shot 闭环原料）
CREATE TABLE IF NOT EXISTS query_history (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    user_id       BIGINT UNSIGNED NOT NULL COMMENT '所属用户 → user.id',
    thread_id     VARCHAR(64)     NOT NULL COMMENT '前端生成的会话 uuid（原文存储）',
    question      TEXT            NOT NULL COMMENT '用户问题原文',
    answer_text   MEDIUMTEXT      NULL COMMENT '最终回答全文（Java 重组 delta 事件得到）',
    generated_sql TEXT            NULL COMMENT '本轮最后一次 execute_readonly_sql 的 SQL（W5 消费）',
    status        TINYINT         NOT NULL COMMENT '1 成功 / 2 失败 / 3 用户中断',
    error_code    VARCHAR(32)     NULL COMMENT '失败或中断时的业务错误码（§1.6 表）',
    latency_ms    INT             NULL COMMENT 'Agent 轮次耗时（done 事件携带）',
    token_in      INT             NULL COMMENT '输入 token（done 事件携带，usage_metadata 汇总）',
    token_out     INT             NULL COMMENT '输出 token',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_qh_user_time (user_id, created_at) COMMENT '历史页：按用户+时间倒序分页',
    KEY idx_qh_thread (user_id, thread_id)     COMMENT '按会话聚合查看'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='查询历史与审计';

-- 4. 反馈表（前端直调；W5 消费点赞样本）
CREATE TABLE IF NOT EXISTS feedback (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    history_id BIGINT UNSIGNED NOT NULL COMMENT '→ query_history.id',
    user_id    BIGINT UNSIGNED NOT NULL COMMENT '冗余列：越权二次校验 + W5 按用户统计',
    rating     TINYINT         NOT NULL COMMENT '1 点赞 / 0 点踩',
    comment    VARCHAR(500)    NULL COMMENT '可选补充（W4 前端不加输入框，字段预留）',
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_feedback_history (history_id) COMMENT '一条回答一个反馈，重复点击走更新',
    KEY idx_feedback_rating (rating) COMMENT 'W5 筛选点赞样本'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='问答反馈';
