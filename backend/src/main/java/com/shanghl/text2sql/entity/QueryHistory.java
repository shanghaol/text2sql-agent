package com.shanghl.text2sql.entity;

import java.time.LocalDateTime;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 查询历史表实体（Java 代理流结束时写入，设计 §2.5 步骤 6）。
 * status：1 成功 / 2 失败 / 3 用户中断。
 */
@TableName("query_history")
public class QueryHistory {

    public static final int STATUS_SUCCESS = 1;
    public static final int STATUS_FAILED = 2;
    public static final int STATUS_ABORTED = 3;

    @TableId(type = IdType.AUTO)
    private Long id;

    @TableField("user_id")
    private Long userId;

    @TableField("thread_id")
    private String threadId;

    private String question;

    @TableField("answer_text")
    private String answerText;

    @TableField("generated_sql")
    private String generatedSql;

    private Integer status;

    @TableField("error_code")
    private String errorCode;

    @TableField("latency_ms")
    private Integer latencyMs;

    @TableField("token_in")
    private Integer tokenIn;

    @TableField("token_out")
    private Integer tokenOut;

    private LocalDateTime createdAt;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
    public String getThreadId() { return threadId; }
    public void setThreadId(String threadId) { this.threadId = threadId; }
    public String getQuestion() { return question; }
    public void setQuestion(String question) { this.question = question; }
    public String getAnswerText() { return answerText; }
    public void setAnswerText(String answerText) { this.answerText = answerText; }
    public String getGeneratedSql() { return generatedSql; }
    public void setGeneratedSql(String generatedSql) { this.generatedSql = generatedSql; }
    public Integer getStatus() { return status; }
    public void setStatus(Integer status) { this.status = status; }
    public String getErrorCode() { return errorCode; }
    public void setErrorCode(String errorCode) { this.errorCode = errorCode; }
    public Integer getLatencyMs() { return latencyMs; }
    public void setLatencyMs(Integer latencyMs) { this.latencyMs = latencyMs; }
    public Integer getTokenIn() { return tokenIn; }
    public void setTokenIn(Integer tokenIn) { this.tokenIn = tokenIn; }
    public Integer getTokenOut() { return tokenOut; }
    public void setTokenOut(Integer tokenOut) { this.tokenOut = tokenOut; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
}
