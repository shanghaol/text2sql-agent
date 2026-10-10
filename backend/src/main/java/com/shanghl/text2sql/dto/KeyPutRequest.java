package com.shanghl.text2sql.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

/**
 * BYOK 录入请求（设计 §1.2 五步流程第 1-2 步）。
 * 格式预检 ^sk-[0-9a-zA-Z]{20,}$：宽松匹配，只挡手滑，不冒充权威校验——
 * 前端同样预检（不过就地红字不发请求），Java 端再查一道是防绕过前端。
 */
public record KeyPutRequest(
        @NotBlank(message = "key 不能为空")
        @Pattern(regexp = "^sk-[0-9a-zA-Z]{20,}$", message = "Key 格式不正确（应以 sk- 开头，后接至少 20 位字母/数字）")
        String key) {
}
