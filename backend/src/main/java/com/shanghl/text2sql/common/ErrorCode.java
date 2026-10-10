package com.shanghl.text2sql.common;

/**
 * 业务错误码 → HTTP 状态映射（W4 设计 §1.6 / §2.4 / §3.3 三层错误语义表的 Java 侧全集）。
 * 约定：枚举名大写（Java 惯例），wireCode 是对外统一的业务错误码字符串（§3.3 表小写蛇形），
 * 前端与 Python 侧只认 wireCode。
 */
public enum ErrorCode {
    // ---- 认证（§2.3）----
    AUTH_REQUIRED("auth_required", 401, "登录状态缺失、无效或已过期"),
    INVALID_REFRESH("invalid_refresh", 401, "refresh token 无效或已过期"),
    BAD_CREDENTIALS("bad_credentials", 401, "用户名或密码错误"),
    USERNAME_TAKEN("username_taken", 409, "用户名已被占用"),

    // ---- BYOK（§1.2 错误文案表）----
    BAD_KEY_FORMAT("bad_key_format", 400, "Key 格式不正确（应以 sk- 开头，后接至少 20 位字母/数字）"),
    KEY_INVALID("key_invalid", 400, "Key 无效或已被删除，请到 DeepSeek 平台核对"),
    VERIFY_RATE_LIMITED("verify_rate_limited", 429, "校验请求过于频繁，请一分钟后再试"),
    VERIFY_UNREACHABLE("verify_unreachable", 502, "DeepSeek 校验服务暂时不可达，请稍后重试（本次未保存）"),
    KEY_NOT_SET("key_not_set", 403, "尚未配置 DeepSeek Key，请先到设置页录入"),
    MASTER_KEY_MISMATCH("master_key_mismatch", 500, "主密钥不匹配，请联系管理员重置 key"),

    // ---- 网关限流 / 并发（§1.7）----
    RATE_LIMITED("rate_limited", 429, "请求过于频繁，请稍后再试"),
    CONCURRENCY_FULL("concurrency_full", 503, "当前使用人数较多，请稍后再试"),

    // ---- 通用（§3.3）----
    BAD_REQUEST("bad_request", 422, "请求参数不合法"),
    HISTORY_NOT_FOUND("history_not_found", 404, "历史记录不存在"),
    FORBIDDEN("forbidden", 403, "无权访问"),
    UPSTREAM_UNREACHABLE("upstream_unreachable", 502, "上游服务暂时不可用，请稍后重试"),
    UPSTREAM_ERROR("upstream_error", 502, "上游服务返回错误，请稍后重试"),
    INTERNAL_ERROR("internal_error", 500, "服务内部错误，请稍后重试");

    /** 对外（HTTP 响应体 / 跨服务）的业务错误码，与设计 §3.3 汇总表一致。 */
    public final String wireCode;
    public final int httpStatus;
    public final String defaultMessage;

    ErrorCode(String wireCode, int httpStatus, String defaultMessage) {
        this.wireCode = wireCode;
        this.httpStatus = httpStatus;
        this.defaultMessage = defaultMessage;
    }
}
