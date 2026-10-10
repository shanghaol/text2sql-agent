package com.shanghl.text2sql.dto;

import java.time.LocalDateTime;

/** 历史列表行（§2.4：分页列表只给摘要，全文走 /api/history/{id}）。 */
public record HistorySummary(long id, String question, int status, LocalDateTime createdAt) {
}
