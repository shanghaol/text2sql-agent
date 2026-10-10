package com.shanghl.text2sql.controller;

import java.util.List;

import com.shanghl.text2sql.common.ApiResponse;
import com.shanghl.text2sql.dto.InternalHistoryRow;
import com.shanghl.text2sql.dto.InternalSearchResult;
import com.shanghl.text2sql.service.HistoryService;

import jakarta.validation.constraints.Positive;

import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * 内部检索接口（设计 §2.5）：给 Python 侧 search_query_history @tool 调用——
 * "Agent 跨语言调 Java REST"叙事的 Java 侧落点（只读、低风险）。
 * 鉴权不走 JWT：X-Internal-Token（Java/Python 共享密钥，常量时间比较）+ X-User-Id
 * （Java 鉴权体系里由 Python 从自己的请求头透传回来）。配置为空 = 不校验（仅限开发机直连调试）。
 */
@RestController
@RequestMapping("/api/internal")
@Validated
public class InternalController {

    private final HistoryService historyService;
    private final String internalToken;

    public InternalController(HistoryService historyService,
                              com.shanghl.text2sql.config.AppProperties props) {
        this.historyService = historyService;
        this.internalToken = props.getInternalToken() == null ? "" : props.getInternalToken();
    }

    @GetMapping("/history/search")
    public ApiResponse<InternalSearchResult> search(
            @RequestParam String keyword,
            @RequestParam(defaultValue = "10") @Positive(message = "limit 必须为正数") int limit,
            @RequestHeader(value = "X-Internal-Token", required = false) String token,
            @RequestHeader(value = "X-User-Id", required = false) String userIdHeader) {
        // 常量时间比较，避免 token 逐字节比对的时序侧信道
        if (!tokenMatches(token)) {
            throw new com.shanghl.text2sql.common.BizException(com.shanghl.text2sql.common.ErrorCode.FORBIDDEN,
                    "内部头缺失或 X-Internal-Token 错误");
        }
        long userId;
        try {
            if (userIdHeader == null || userIdHeader.isBlank()) {
                throw new NumberFormatException("blank");
            }
            userId = Long.parseLong(userIdHeader.trim());
        } catch (NumberFormatException e) {
            throw new com.shanghl.text2sql.common.BizException(com.shanghl.text2sql.common.ErrorCode.FORBIDDEN,
                    "X-User-Id 缺失或非法");
        }
        List<InternalHistoryRow> records = historyService.searchKeyword(userId, keyword, limit).stream()
                .map(h -> new InternalHistoryRow(h.getId(), h.getThreadId(), h.getQuestion(),
                        h.getAnswerText(), h.getStatus(), h.getCreatedAt()))
                .toList();
        return ApiResponse.ok(new InternalSearchResult(records.size(), records));
    }

    private boolean tokenMatches(String provided) {
        if (internalToken.isEmpty()) {
            return true; // 未配置 token：不校验（§3.4 开发机语义；生产必配，§5.5 核对清单）
        }
        if (provided == null) {
            return false;
        }
        return MessageDigest.isEqual(internalToken.getBytes(StandardCharsets.UTF_8),
                provided.getBytes(StandardCharsets.UTF_8));
    }
}
