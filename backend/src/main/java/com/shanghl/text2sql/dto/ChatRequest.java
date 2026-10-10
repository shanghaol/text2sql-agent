package com.shanghl.text2sql.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * 对话请求（设计 §2.4）：question ≤2000 字符；threadId 为前端生成的会话 uuid。
 * threadId 的 uuid 格式校验在 ChatProxyService（UUID.fromString），非法 → 422 bad_request。
 */
public record ChatRequest(
        @NotBlank(message = "question 不能为空")
        @Size(max = 2000, message = "question 不能超过 2000 字符")
        String question,

        @NotBlank(message = "threadId 不能为空")
        String threadId) {

    /** 供测试/手动构造。 */
    public ChatRequest {
    }
}
