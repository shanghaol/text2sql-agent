package com.shanghl.text2sql.dto;

/**
 * 认证数据（登录/刷新响应；注册即登录所以注册也返回同构结构，设计 §2.3/§2.4）。
 */
public record AuthData(long userId, String accessToken, String refreshToken, UserView user) {
}
