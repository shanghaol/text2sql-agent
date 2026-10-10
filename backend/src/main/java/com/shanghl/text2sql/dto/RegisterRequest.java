package com.shanghl.text2sql.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * 注册请求（设计 §2.3）：用户名 ^[A-Za-z0-9_]{4,32}$ + 密码 ≥8 位。
 * 密码上限 72：BCrypt 只取前 72 字节，超过部分会被静默截断，因此直接在入口拒绝。
 */
public record RegisterRequest(
        @NotBlank(message = "用户名不能为空")
        @Pattern(regexp = "^[A-Za-z0-9_]{4,32}$", message = "用户名需为 4-32 位字母、数字或下划线")
        String username,

        @NotBlank(message = "密码不能为空")
        @Size(min = 8, max = 72, message = "密码长度需为 8-72 位")
        String password) {
}
