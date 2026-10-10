package com.shanghl.text2sql.service;

import java.util.List;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.dto.HistoryDetail;
import com.shanghl.text2sql.dto.HistoryPage;
import com.shanghl.text2sql.dto.HistorySummary;
import com.shanghl.text2sql.entity.QueryHistory;
import com.shanghl.text2sql.mapper.QueryHistoryMapper;

import org.springframework.stereotype.Service;

/**
 * 查询历史（设计 §2.4）：分页列表 / 单条详情。
 * 越权防护红线（§1.7）：一律 WHERE user_id = 当前 JWT 用户；详情非本人 → 404
 * （与"不存在"同响应，不泄露他人记录存在性）。
 */
@Service
public class HistoryService {

    private static final int DEFAULT_SIZE = 20;
    private static final int MAX_SIZE = 100;

    private final QueryHistoryMapper historyMapper;

    public HistoryService(QueryHistoryMapper historyMapper) {
        this.historyMapper = historyMapper;
    }

    public HistoryPage page(long userId, int page, int size) {
        int p = Math.max(page, 1);
        int s = size <= 0 ? DEFAULT_SIZE : Math.min(size, MAX_SIZE);
        Page<QueryHistory> result = historyMapper.selectPage(
                new Page<>(p, s),
                Wrappers.<QueryHistory>lambdaQuery()
                        .eq(QueryHistory::getUserId, userId)
                        .orderByDesc(QueryHistory::getId)); // id 倒序 ≈ created_at 倒序（自增主键，避免同秒乱序）
        List<HistorySummary> records = result.getRecords().stream()
                .map(h -> new HistorySummary(h.getId(), h.getQuestion(), h.getStatus(), h.getCreatedAt()))
                .toList();
        return new HistoryPage(result.getTotal(), records);
    }

    public HistoryDetail detail(long userId, long id) {
        QueryHistory h = historyMapper.selectById(id);
        if (h == null || h.getUserId() == null || h.getUserId() != userId) {
            throw new BizException(ErrorCode.HISTORY_NOT_FOUND); // 非本人与不存在同响应（§2.4）
        }
        return new HistoryDetail(h.getId(), h.getUserId(), h.getThreadId(), h.getQuestion(),
                h.getAnswerText(), h.getGeneratedSql(), h.getStatus(), h.getErrorCode(),
                h.getLatencyMs(), h.getTokenIn(), h.getTokenOut(), h.getCreatedAt());
    }

    public List<QueryHistory> searchKeyword(long userId, String rawKeyword, int limit) {
        String keyword = rawKeyword == null ? "" : rawKeyword.trim();
        if (keyword.isEmpty()) {
            return List.of();
        }
        int capped = Math.max(1, Math.min(limit, 50)); // 内部检索上限 50（§2.5 只读低风险）
        // LIKE 转义：\ % _ 全部转义，防止关键字里带通配符改变匹配语义
        String escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_");
        return historyMapper.searchByKeyword(userId, escaped, capped);
    }
}
