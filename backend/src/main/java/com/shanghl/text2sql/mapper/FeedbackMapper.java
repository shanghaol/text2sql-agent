package com.shanghl.text2sql.mapper;

import java.util.List;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.shanghl.text2sql.dto.InternalFeedbackSample;
import com.shanghl.text2sql.entity.Feedback;

import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface FeedbackMapper extends BaseMapper<Feedback> {

    /**
     * 反馈自举样本查询（W5 设计 §4.2）：点赞（rating=1）+ 执行成功（status=1）
     * + 有生成 SQL 的历史，JOIN 两表一次裁出脚本需要的最小字段集。
     * 全库聚合、无 user_id 过滤——消费方是 X-Internal-Token 鉴权的管理脚本，
     * 不是用户自助接口；只读且 limit 由服务层钳制在 [1,200]。
     * id 倒序 ≈ 时间倒序（自增主键，与 HistoryService.page 同款口径）。
     */
    @Select("SELECT h.id AS history_id, h.question, h.generated_sql, f.comment, h.created_at "
            + "FROM feedback f JOIN query_history h ON h.id = f.history_id "
            + "WHERE f.rating = 1 AND h.status = 1 AND h.generated_sql IS NOT NULL "
            + "ORDER BY h.id DESC LIMIT #{limit}")
    List<InternalFeedbackSample> selectFeedbackSamples(@Param("limit") int limit);
}
