package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/** 历史单条详情（§2.4：含 answer_text、generated_sql、latency、tokens）。 */
public record HistoryDetail(long id, long userId, String threadId, String question, String answerText,
                            String generatedSql, int status, String errorCode, Integer latencyMs,
                            Integer tokenIn, Integer tokenOut, LocalDateTime createdAt) {
}
