package com.shanghl.text2sql.common;

/**
 * 业务异常：携带 ErrorCode（或临时指定的 status+code），由 GlobalExceptionHandler
 * 统一渲染为 {code, message, data} JSON。任何业务失败都必须走这里，禁止直接向响应写裸错误。
 */
public class BizException extends RuntimeException {

    private final int httpStatus;
    private final String code;

    public BizException(ErrorCode errorCode) {
        this(errorCode, errorCode.defaultMessage);
    }

    public BizException(ErrorCode errorCode, String message) {
        super(message);
        this.httpStatus = errorCode.httpStatus;
        this.code = errorCode.name();
    }

    /** 上游（Python）非 200 时的透传构造：转发上游 HTTP 状态与业务码（§2.5 步骤 4）。 */
    public BizException(int httpStatus, String code, String message) {
        super(message);
        this.httpStatus = httpStatus;
        this.code = code;
    }

    public int httpStatus() {
        return httpStatus;
    }

    public String code() {
        return code;
    }
}
