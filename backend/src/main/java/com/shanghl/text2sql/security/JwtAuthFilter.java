package com.shanghl.text2sql.security;

import java.io.IOException;
import java.util.List;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.shanghl.text2sql.common.ApiResponse;

import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * JWT 过滤器（设计 §2.3）：放行 /api/auth/**、/api/health、/api/internal/**（内部接口用
 * X-Internal-Token 自鉴权，见 InternalController），其余解析 Bearer 并注入 SecurityContext。
 * 解析失败 → 直接写 401 {code:"auth_required"}（不进入过滤器链）。
 */
@Component
public class JwtAuthFilter extends OncePerRequestFilter {

    private final JwtService jwtService;
    private final ObjectMapper objectMapper;

    public JwtAuthFilter(JwtService jwtService, ObjectMapper objectMapper) {
        this.jwtService = jwtService;
        this.objectMapper = objectMapper;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getRequestURI();
        return path.startsWith("/api/auth/")
                || path.equals("/api/health")
                || path.startsWith("/api/internal/")
                || path.equals("/error");
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String header = request.getHeader("Authorization");
        if (header != null && header.startsWith("Bearer ")) {
            try {
                Claims claims = jwtService.parseAccess(header.substring(7).trim());
                CurrentUser user = new CurrentUser(Long.parseLong(claims.getSubject()),
                        claims.get("username", String.class));
                UsernamePasswordAuthenticationToken auth =
                        new UsernamePasswordAuthenticationToken(user, null, List.of());
                SecurityContextHolder.getContext().setAuthentication(auth);
                try {
                    chain.doFilter(request, response);
                } finally {
                    SecurityContextHolder.clearContext(); // 无状态，请求结束即清
                }
                return;
            } catch (JwtException | IllegalArgumentException e) {
                // 过期 / 篡改 / 格式错 → 落到下方统一 401
            }
        }
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json;charset=UTF-8");
        response.getWriter().write(objectMapper.writeValueAsString(
                ApiResponse.error("auth_required", "登录状态缺失、无效或已过期")));
    }
}
