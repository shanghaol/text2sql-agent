package com.shanghl.text2sql.controller;

import com.shanghl.text2sql.common.ApiResponse;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.dto.AuthData;
import com.shanghl.text2sql.dto.LoginRequest;
import com.shanghl.text2sql.dto.RefreshRequest;
import com.shanghl.text2sql.dto.RegisterRequest;
import com.shanghl.text2sql.dto.UserView;
import com.shanghl.text2sql.security.CurrentUser;
import com.shanghl.text2sql.service.AuthService;
import com.shanghl.text2sql.service.RateLimiter;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 认证三接口 + me（设计 §2.3/§2.4）。
 * 登录/注册按 IP 限流 10 次/分（X-Forwarded-For 首值——Caddy 会注入，直连时取 remoteAddr）。
 */
@RestController
public class AuthController {

    private final AuthService authService;
    private final RateLimiter rateLimiter;
    private final int authPerMin;

    public AuthController(AuthService authService, RateLimiter rateLimiter,
                          com.shanghl.text2sql.config.AppProperties props) {
        this.authService = authService;
        this.rateLimiter = rateLimiter;
        this.authPerMin = props.getChat().getAuthRateLimitPerMin();
    }

    /** 注册即登录：直接发双 token（§2.3，省一步）。 */
    @PostMapping("/api/auth/register")
    public ApiResponse<AuthData> register(@Valid @RequestBody RegisterRequest req,
                                          HttpServletRequest request) {
        rateLimiter.check("auth:" + clientIp(request), ErrorCode.RATE_LIMITED, authPerMin, 60_000L);
        return ApiResponse.ok(authService.register(req));
    }

    @PostMapping("/api/auth/login")
    public ApiResponse<AuthData> login(@Valid @RequestBody LoginRequest req,
                                       HttpServletRequest request) {
        rateLimiter.check("auth:" + clientIp(request), ErrorCode.RATE_LIMITED, authPerMin, 60_000L);
        return ApiResponse.ok(authService.login(req));
    }

    /** refresh 旋转换发（前端在 access 过期 401 时静默调用并重放原请求，§4.3）。 */
    @PostMapping("/api/auth/refresh")
    public ApiResponse<AuthData> refresh(@Valid @RequestBody RefreshRequest req) {
        return ApiResponse.ok(authService.refresh(req));
    }

    /** 启动时恢复会话（§2.4）。 */
    @GetMapping("/api/me")
    public ApiResponse<UserView> me(@AuthenticationPrincipal CurrentUser user) {
        return ApiResponse.ok(authService.me(user.id()));
    }

    /**
     * 客户端真实 IP（审查 P1-b 修复）：优先读 nginx 注入的 X-Real-IP——
     * 它由 $remote_addr 生成、客户端无法伪造；XFF 首值是客户端可控字段，
     * 直接采用等于登录限流可被伪造头绕过（审查实测 12 次登录全 401 从不 429）。
     * 直连开发机（无反代、无 X-Real-IP）时退回 remoteAddr。
     */
    static String clientIp(HttpServletRequest request) {
        String realIp = request.getHeader("X-Real-IP");
        if (realIp != null && !realIp.isBlank()) {
            return realIp.trim();
        }
        return request.getRemoteAddr();
    }
}
