package com.shanghl.text2sql.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.shanghl.text2sql.common.ApiResponse;
import com.shanghl.text2sql.security.JwtAuthFilter;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

/**
 * Spring Security 6 过滤器链（设计 §2.3）：
 * - 无状态（STATELESS）+ 关 CSRF（纯 token API，无 cookie 会话）；
 * - JwtAuthFilter 放行 /api/auth/** 与 /api/health（内部接口 /api/internal/** 用 X-Internal-Token 自鉴权）；
 * - 其余请求必须携带有效 Bearer access token，失败统一 401 {code:"auth_required"}。
 *
 * 备选降级路径（设计 §2.1 记录在案）：Security 配置若严重挤占工期，可降级为 HandlerInterceptor
 * + jjwt 手写鉴权；本实现未触发该降级。
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http, JwtAuthFilter jwtAuthFilter) throws Exception {
        http.csrf(csrf -> csrf.disable())
            .sessionManagement(sm -> sm.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .formLogin(form -> form.disable())
            .httpBasic(basic -> basic.disable())
            .authorizeHttpRequests(auth -> auth
                    .requestMatchers("/api/auth/**", "/api/health", "/api/internal/**", "/error").permitAll()
                    .anyRequest().authenticated())
            .exceptionHandling(eh -> eh.authenticationEntryPoint((request, response, ex) -> {
                // 未带 token / 过期 token 打受保护接口 → 401 auth_required（§3.3）
                response.setStatus(401);
                response.setContentType("application/json;charset=UTF-8");
                response.getWriter().write(new ObjectMapper()
                        .writeValueAsString(ApiResponse.error("auth_required", "登录状态缺失、无效或已过期")));
            }))
            .addFilterBefore(jwtAuthFilter, UsernamePasswordAuthenticationFilter.class);
        return http.build();
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        // BCrypt strength 10（默认）。哈希 60 字符，DDL password_hash VARCHAR(100) 留有余量
        return new BCryptPasswordEncoder();
    }
}
