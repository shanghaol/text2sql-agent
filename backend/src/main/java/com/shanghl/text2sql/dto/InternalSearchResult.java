package com.shanghl.text2sql.dto;

import java.util.List;

/** 内部检索响应（GET /api/internal/history/search）。 */
public record InternalSearchResult(int total, List<InternalHistoryRow> records) {
}
