package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/**
 * 内部检索单行（GET /api/internal/history/search，设计 §2.5）——
 * 消费方是 Python 侧 search_query_history @tool（X-Internal-Token + X-User-Id 已校验）。
 */
public record InternalHistoryRow(long id, String threadId, String question, String answerText,
                                 int status, LocalDateTime createdAt) {
}
