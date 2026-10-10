package com.shanghl.text2sql.controller;

import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 健康检查（设计 §2.4：Caddy/运维探活，Java 与 MySQL 连通性）。
 * 轻量：只 SELECT 1，不查出站服务（出站调用不能当探活，§3.4 同理）。
 */
@RestController
public class HealthController {

    private final JdbcTemplate jdbcTemplate;

    public HealthController(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    @GetMapping("/api/health")
    public Map<String, String> health() {
        try {
            jdbcTemplate.queryForObject("SELECT 1", Integer.class);
            return Map.of("status", "up");
        } catch (Exception e) {
            // MySQL 不可达：503 down（compose healthcheck 消费）
            return Map.of("status", "down");
        }
    }
}
