package com.shanghl.text2sql.dto;

import java.util.List;

/** 历史分页响应（§2.4）。 */
public record HistoryPage(long total, List<HistorySummary> records) {
}
