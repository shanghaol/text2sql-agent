package com.shanghl.text2sql.dto;

import jakarta.validation.constraints.NotNull;

/**
 * 用户设置更新请求（W5 设计 §3.8）：PUT /api/user/settings。
 * 目前只有 HITL 一档（§9-1：粒度 = 是否确认 execute_readonly_sql）。
 */
public record UserSettingsRequest(
        @NotNull(message = "hitlEnabled 不能为空") Boolean hitlEnabled) {
}
