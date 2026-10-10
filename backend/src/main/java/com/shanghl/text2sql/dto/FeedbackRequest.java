package com.shanghl.text2sql.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

/**
 * 反馈请求（§2.4）：rating 1 点赞 / 0 点踩；upsert（唯一键 uk_feedback_history）。
 * 越权按 user_id 二次校验（history 不属于当前用户 → history_not_found）。
 */
public record FeedbackRequest(
        @NotNull(message = "historyId 不能为空")
        Long historyId,

        @NotNull(message = "rating 不能为空")
        @Min(value = 0, message = "rating 只能为 0（点踩）或 1（点赞）")
        @Max(value = 1, message = "rating 只能为 0（点踩）或 1（点赞）")
        Integer rating,

        @Size(max = 500, message = "comment 最多 500 字符")
        String comment) {
}
