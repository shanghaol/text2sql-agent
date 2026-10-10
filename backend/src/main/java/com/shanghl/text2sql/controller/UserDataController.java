package com.shanghl.text2sql.controller;

import java.util.Map;

import com.shanghl.text2sql.common.ApiResponse;
import com.shanghl.text2sql.dto.FeedbackRequest;
import com.shanghl.text2sql.dto.HistoryDetail;
import com.shanghl.text2sql.dto.HistoryPage;
import com.shanghl.text2sql.security.CurrentUser;
import com.shanghl.text2sql.service.FeedbackService;
import com.shanghl.text2sql.service.HistoryService;

import jakarta.validation.Valid;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 历史与反馈 REST（设计 §2.4）。越权红线（§1.7）：一律 WHERE user_id = 当前 JWT 用户。
 */
@RestController
public class UserDataController {

    private final HistoryService historyService;
    private final FeedbackService feedbackService;

    public UserDataController(HistoryService historyService, FeedbackService feedbackService) {
        this.historyService = historyService;
        this.feedbackService = feedbackService;
    }

    /** 历史分页：倒序；size 默认 20（§2.4）。 */
    @GetMapping("/api/history")
    public ApiResponse<HistoryPage> history(@AuthenticationPrincipal CurrentUser user,
                                            @RequestParam(defaultValue = "1") int page,
                                            @RequestParam(defaultValue = "20") int size) {
        return ApiResponse.ok(historyService.page(user.id(), page, size));
    }

    /** 历史单条详情（含 answer_text、generated_sql、latency、tokens）；非本人 → 404。 */
    @GetMapping("/api/history/{id}")
    public ApiResponse<HistoryDetail> historyDetail(@AuthenticationPrincipal CurrentUser user,
                                                    @PathVariable long id) {
        return ApiResponse.ok(historyService.detail(user.id(), id));
    }

    /** 反馈 upsert（唯一键 uk_feedback_history，重复点击走更新）；越权 → history_not_found。 */
    @PostMapping("/api/feedback")
    public ApiResponse<Map<String, Boolean>> feedback(@Valid @RequestBody FeedbackRequest req,
                                                      @AuthenticationPrincipal CurrentUser user) {
        feedbackService.upsert(user.id(), req.historyId(), req.rating(), req.comment());
        return ApiResponse.ok(Map.of("saved", true));
    }
}
