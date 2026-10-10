package com.shanghl.text2sql.mapper;

import java.util.List;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.shanghl.text2sql.entity.QueryHistory;

import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface QueryHistoryMapper extends BaseMapper<QueryHistory> {

    /**
     * 内部检索接口（设计 §2.5：search_query_history @tool 的 Java 侧）——
     * 按用户隔离（WHERE user_id=）+ LIKE 检索本人历史，给 Python 侧调用。
     * 关键字已由服务层做 LIKE 转义（\ % _），ESCAPE '\\' 显式声明转义符。
     * 结果按 id 倒序（时间倒序近似），limit 由服务层钳制在 [1,50]。
     */
    @Select("SELECT id, user_id, thread_id, question, answer_text, generated_sql, status, "
            + "error_code, latency_ms, token_in, token_out, created_at "
            + "FROM query_history "
            + "WHERE user_id = #{userId} "
            + "  AND (question LIKE CONCAT('%', #{keyword}, '%') ESCAPE '\\\\' "
            + "       OR answer_text LIKE CONCAT('%', #{keyword}, '%') ESCAPE '\\\\') "
            + "ORDER BY id DESC LIMIT #{limit}")
    List<QueryHistory> searchByKeyword(@Param("userId") long userId,
                                       @Param("keyword") String escapedKeyword,
                                       @Param("limit") int limit);
}
