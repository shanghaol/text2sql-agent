package com.shanghl.text2sql.entity;

import java.time.LocalDateTime;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 用户表实体（DDL 见 schema-app.sql 表 1）。
 * created_at/updated_at 由 MySQL 默认值维护：插入时保持 null（MP 默认策略跳过 null 字段）。
 * hitl_enabled（W5 §3.8）：SQL 执行前人工确认开关，TINYINT 0/1——代理层据此注入 X-User-HITL 头。
 */
@TableName("`user`")
public class User {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String username;

    @TableField("password_hash")
    private String passwordHash;

    /** SQL 执行前人工确认（W5 HITL）：0 关 / 1 开，DDL 默认 0（§3.8）。 */
    @TableField("hitl_enabled")
    private Integer hitlEnabled;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getUsername() { return username; }
    public void setUsername(String username) { this.username = username; }
    public String getPasswordHash() { return passwordHash; }
    public void setPasswordHash(String passwordHash) { this.passwordHash = passwordHash; }
    public Integer getHitlEnabled() { return hitlEnabled; }
    public void setHitlEnabled(Integer hitlEnabled) { this.hitlEnabled = hitlEnabled; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
    public LocalDateTime getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(LocalDateTime updatedAt) { this.updatedAt = updatedAt; }
}
