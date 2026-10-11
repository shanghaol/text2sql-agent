package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/**
 * 反馈自举样本行（GET /api/internal/feedback/samples，W5 设计 §4.2）——
 * 消费方是 Python 侧 agent/rag/absorb_feedback.py（点赞 → few-shot 语料）。
 * 字段是最小集：不含 user_id / thread_id / answer_text（样本聚合脚本用不到，
 * 也不该把用户身份明细扩散到语料链路，§6 风险 7）。
 *
 * 用可变 POJO 而非 record：MyBatis 默认按"无参构造 + setter"映射查询结果，
 * record 没有无参构造，直接作为 mapper 返回类型会映射失败。
 */
public class InternalFeedbackSample {

    private Long historyId;

    private String question;

    private String generatedSql;

    private String comment;

    private LocalDateTime createdAt;

    public Long getHistoryId() { return historyId; }
    public void setHistoryId(Long historyId) { this.historyId = historyId; }
    public String getQuestion() { return question; }
    public void setQuestion(String question) { this.question = question; }
    public String getGeneratedSql() { return generatedSql; }
    public void setGeneratedSql(String generatedSql) { this.generatedSql = generatedSql; }
    public String getComment() { return comment; }
    public void setComment(String comment) { this.comment = comment; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
}
