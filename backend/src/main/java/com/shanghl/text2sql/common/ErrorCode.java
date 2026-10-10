package com.shanghl.text2sql.common;

/**
 * 业务错误码 → HTTP 状态映射（W4 设计 §1.6 / §2.4 / §3.3 三层错误语义表的 Java 侧全集）。
 * 约定：枚举名即业务错误码字符串，httpStatus 供响应使用。
 */
public enum ErrorCode {
    // ---- 认证（§2.3）----
    AUTH_REQUIRED(401, "登录状态缺失、无效或已过期"),
    INVALID_REFRESH(401, "refresh token 无效或已过期"),
    BAD_CREDENTIALS(401, "用户名或密码错误"),
    USERNAME_TAKEN(409, "用户名已被占用"),

    // ---- BYOK（§1.2 错误文案表）----
    BAD_KEY_FORMAT(400, "Key 格式不正确（应以 sk- 开头，后接至少 20 位字母/数字）"),
    KEY_INVALID(400, "Key 无效或已被删除，请到 DeepSeek 平台核对"),
    VERIFY_RATE_LIMITED(429, "校验请求过于频繁，请一分钟后再试"),
    VERIFY_UNREACHABLE(502, "DeepSeek 校验服务暂时不可达，请稍后重试（本次未保存）"),
    KEY_NOT_SET(403, "尚未配置 DeepSeek Key，请先到设置页录入"),
    MASTER_KEY_MISMATCH(500, "主密钥不匹配，请联系管理员重置 key"),

    // ---- 网关限流 / 并发（§1.7）----
    RATE_LIMITED(429, "请求过于频繁，请稍后再试"),
    CONCURRENCY_FULL(503, "当前使用人数较多，请稍后再试"),

    // ---- 通用（§3.3）----
    BAD_REQUEST(422, "请求参数不合法"),
    HISTORY_NOT_FOUND(404, "历史记录不存在"),
    FORBIDDEN(403, "无权访问"),
    UPSTREAM_UNREACHABLE(502, "上游服务暂时不可用，请稍后重试"),
    UPSTREAM_ERROR(502, "上游服务返回错误，请稍后重试"),
    INTERNAL_ERROR(500, "服务内部错误，请稍后重试");

    public final int httpStatus;
    public final String defaultMessage;

    ErrorCode(int httpStatus, String defaultMessage) {
        this.httpStatus = httpStatus;
        this.defaultMessage = defaultMessage;
    }
}
