package com.shanghl.text2sql.security;

/**
 * JWT 鉴权通过后的当前用户（作为 Authentication 的 principal 注入 SecurityContext）。
 * Controller 用 @AuthenticationPrincipal CurrentUser 取身份，避免各处重复解析 claims。
 */
public record CurrentUser(long id, String username) {
}
