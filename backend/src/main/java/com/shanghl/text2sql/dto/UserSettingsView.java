package com.shanghl.text2sql.dto;

/**
 * 用户设置视图（W5 设计 §3.8）：GET /api/user/settings 的 data。
 */
public record UserSettingsView(boolean hitlEnabled) {
}
