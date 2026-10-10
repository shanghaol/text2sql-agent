package com.shanghl.text2sql.common;

/**
 * 统一响应体（W4 设计 §2.4）：{code, message, data}。
 * code = "ok" 表示成功；错误时 code 即 §1.6/§3.3 汇总表中的业务错误码，
 * 前端据此做文案映射（错误码-文案表是前后端唯一契约）。
 */
public record ApiResponse<T>(String code, String message, T data) {

    public static <T> ApiResponse<T> ok(T data) {
        return new ApiResponse<>("ok", "success", data);
    }

    public static ApiResponse<Void> ok() {
        return new ApiResponse<>("ok", "success", null);
    }

    public static ApiResponse<Void> error(String code, String message) {
        return new ApiResponse<>(code, message, null);
    }
}
