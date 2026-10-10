package com.shanghl.text2sql.common;

import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.validation.BindException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;

/**
 * 全局异常 → 统一 {code, message, data} 响应。
 *
 * 日志脱敏约束（W4 设计 §1.5 规则 1）：BYOK 相关端点（/api/user/key、/api/chat）的请求体
 * 含明文 key，本处理器只记录异常类名与 message，绝不打印请求体；BizException 的 message
 * 均为业务文案，不含 key（各服务已按规则 2 用指纹+长度做日志替身）。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(BizException.class)
    public ResponseEntity<ApiResponse<Void>> biz(BizException e) {
        // rate_limited / verify_rate_limited 按设计 §3.3 附带 Retry-After
        ResponseEntity.BodyBuilder builder = ResponseEntity.status(e.httpStatus());
        if ("rate_limited".equals(e.code()) || "verify_rate_limited".equals(e.code())) {
            builder.header("Retry-After", "60");
        }
        return builder.body(ApiResponse.error(e.code(), e.getMessage()));
    }

    /** 参数校验失败（@Valid 注解）→ 422 bad_request（§3.3：question 超长、字段非法走这里）。 */
    @ExceptionHandler({MethodArgumentNotValidException.class, BindException.class})
    public ResponseEntity<ApiResponse<Void>> invalidBody(BindException e) {
        String detail = e.getBindingResult().getFieldErrors().stream()
                .map(f -> f.getField() + ": " + f.getDefaultMessage())
                .collect(Collectors.joining("; "));
        return ResponseEntity.status(422)
                .body(ApiResponse.error(ErrorCode.BAD_REQUEST.name(), "请求参数不合法: " + detail));
    }

    /** 请求体不是合法 JSON / 缺参数 / 参数类型错 → 422 bad_request。 */
    @ExceptionHandler({HttpMessageNotReadableException.class,
            MissingServletRequestParameterException.class,
            MethodArgumentTypeMismatchException.class})
    public ResponseEntity<ApiResponse<Void>> badPayload(Exception e) {
        log.info("bad request payload: {}", e.getClass().getSimpleName());
        return ResponseEntity.status(422)
                .body(ApiResponse.error(ErrorCode.BAD_REQUEST.name(), ErrorCode.BAD_REQUEST.defaultMessage));
    }

    /** 兜底。日志含堆栈但不含请求体（堆栈里没有请求体内容）。 */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> unexpected(Exception e) {
        log.error("unexpected error: {}", e.toString(), e);
        return ResponseEntity.status(500)
                .body(ApiResponse.error(ErrorCode.INTERNAL_ERROR.name(), ErrorCode.INTERNAL_ERROR.defaultMessage));
    }
}
