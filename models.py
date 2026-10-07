from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Text, Float, Enum as SAEnum, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, date
from database import Base
import enum


class VolunteerStatus(str, enum.Enum):
    PENDING_REVIEW = "报名待审"
    IN_TRAINING = "培训中"
    PENDING_ASSESSMENT = "待考核"
    CERTIFIED = "已持证"
    DISABLED = "已停用"


class AssessmentResult(str, enum.Enum):
    PENDING = "待考核"
    PASSED = "通过"
    FAILED = "未通过"


class TrainingBatchStatus(str, enum.Enum):
    DRAFT = "草稿"
    ENROLLING = "报名中"
    IN_PROGRESS = "进行中"
    COMPLETED = "已完成"
    CANCELLED = "已取消"


class EnrollmentStatus(str, enum.Enum):
    ENROLLED = "已入班"
    DROPPED = "已退班"
    COMPLETED = "已完成培训"


class TimeSlotStatus(str, enum.Enum):
    AVAILABLE = "可认领"
    CLAIMED = "已认领"
    COMPLETED = "已完成"
    CANCELLED = "已取消"


class School(Base):
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    contact_person = Column(String(50))
    contact_phone = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteers = relationship("Volunteer", back_populates="school")


class StarLevel(Base):
    __tablename__ = "star_levels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(20), nullable=False, unique=True)
    min_hours = Column(Float, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteers = relationship("Volunteer", back_populates="star_level")


class PointsType(str, enum.Enum):
    EARN = "获得"
    SPEND = "消耗"


class PointsSource(str, enum.Enum):
    SERVICE_COMPLETION = "完成讲解"
    TEACHER_RATING = "老师好评"
    EXCHANGE_BADGE = "兑换徽章"
    EXCHANGE_PRIORITY_SLOT = "兑换优先时段"
    OTHER = "其他"


class BenefitType(str, enum.Enum):
    BADGE = "纪念徽章"
    PHYSICAL = "实物权益"
    PRIORITY_SLOT = "优先认领时段"
    OTHER = "其他权益"


class ExchangeStatus(str, enum.Enum):
    # 预占流程：RESERVED(已预占积分与库存) -> CONFIRMED(后台确认发放) / REJECTED(审核拒绝)
    #          -> PARTIALLY_FULFILLED(实物部分履约) / FULFILLED(全部履约) / CANCELLED(用户取消/超时释放)
    RESERVED = "已预占"
    CONFIRMED = "已确认"
    PARTIALLY_FULFILLED = "部分履约"
    FULFILLED = "已履约"
    REJECTED = "已拒绝"
    CANCELLED = "已取消"


class ExchangeReleaseReason(str, enum.Enum):
    TIMEOUT = "超时未确认"
    REJECTED = "后台拒绝"
    USER_CANCELLED = "用户取消"
    PARTIAL_SHORTAGE = "部分履约库存不足"
    MANUAL_COMPENSATION = "人工补偿"


class PointsLedgerType(str, enum.Enum):
    # 每一次积分变动都以不可变流水落账，账户余额由流水汇总得出
    EARN = "获得"
    FREEZE = "冻结"
    UNFREEZE = "解冻"
    CONSUME = "消费"           # 冻结转消费（兑换确认/履约）
    SPEND = "直接消耗"         # 非兑换流程直接扣减可用积分（如更正服务记录）
    MANUAL_ADJUST = "人工调整"


class EntitlementStatus(str, enum.Enum):
    # 优先时段券：兑换确认时只发资格，真正在认领时段时才消费
    ISSUED = "已发放"
    USED = "已使用"
    REVOKED = "已收回"


class Volunteer(Base):
    __tablename__ = "volunteers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    gender = Column(String(10))
    birth_date = Column(Date)
    school_id = Column(Integer, ForeignKey("schools.id"))
    grade = Column(String(20))
    parent_name = Column(String(50))
    parent_phone = Column(String(20))
    preferred_topic = Column(String(100))
    status = Column(SAEnum(VolunteerStatus), default=VolunteerStatus.PENDING_REVIEW)
    star_level_id = Column(Integer, ForeignKey("star_levels.id"))
    total_service_hours = Column(Float, default=0.0)
    # points_balance 即可用积分（预占时已扣减）；frozen_points 为冻结积分
    # 账户总积分 = points_balance + frozen_points，二者均由 PointsLedger 流水汇总解释
    points_balance = Column(Integer, default=0)
    frozen_points = Column(Integer, default=0, nullable=False)
    registration_date = Column(Date, default=date.today)
    certification_date = Column(Date)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school = relationship("School", back_populates="volunteers")
    star_level = relationship("StarLevel", back_populates="volunteers")
    trainings = relationship("TrainingAttendance", back_populates="volunteer")
    assessments = relationship("Assessment", back_populates="volunteer")
    time_slots = relationship("TimeSlot", back_populates="volunteer")
    service_records = relationship("ServiceRecord", back_populates="volunteer")
    enrollments = relationship("Enrollment", back_populates="volunteer")
    certifications = relationship("VolunteerCertification", back_populates="volunteer")
    points_records = relationship("PointsRecord", back_populates="volunteer")
    points_ledgers = relationship("PointsLedger", back_populates="volunteer")
    benefit_exchanges = relationship("BenefitExchange", back_populates="volunteer")
    star_certificates = relationship("StarCertificate", back_populates="volunteer")
    entitlements = relationship("PriorityEntitlement", back_populates="volunteer")


class AssessmentTopic(Base):
    __tablename__ = "assessment_topics"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)
    pass_score = Column(Float, default=60.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    criteria = relationship("AssessmentCriterion", back_populates="topic")
    questions = relationship("AssessmentQuestion", back_populates="topic")
    assessments = relationship("Assessment", back_populates="topic_obj")
    certifications = relationship("VolunteerCertification", back_populates="topic")


class TrainingBatch(Base):
    __tablename__ = "training_batches"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    topic_id = Column(Integer, ForeignKey("assessment_topics.id"))
    description = Column(Text)
    min_attendance_rate = Column(Float, default=80.0)
    capacity = Column(Integer, default=30)
    status = Column(SAEnum(TrainingBatchStatus), default=TrainingBatchStatus.DRAFT)
    start_date = Column(Date)
    end_date = Column(Date)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    topic = relationship("AssessmentTopic")
    sessions = relationship("TrainingSession", back_populates="batch", cascade="all, delete-orphan")
    enrollments = relationship("Enrollment", back_populates="batch", cascade="all, delete-orphan")
    assessments = relationship("Assessment", back_populates="training_batch")


class TrainingSession(Base):
    __tablename__ = "training_sessions"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("training_batches.id"), nullable=False)
    session_no = Column(Integer, nullable=False)
    title = Column(String(100), nullable=False)
    session_date = Column(Date, nullable=False)
    start_time = Column(String(10))
    end_time = Column(String(10))
    location = Column(String(100))
    trainer = Column(String(50))
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("TrainingBatch", back_populates="sessions")
    attendances = relationship("SessionAttendance", back_populates="session", cascade="all, delete-orphan")


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("training_batches.id"), nullable=False)
    status = Column(SAEnum(EnrollmentStatus), default=EnrollmentStatus.ENROLLED)
    enrolled_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    notes = Column(Text)

    volunteer = relationship("Volunteer", back_populates="enrollments")
    batch = relationship("TrainingBatch", back_populates="enrollments")
    attendances = relationship("SessionAttendance", back_populates="enrollment", cascade="all, delete-orphan")


class SessionAttendance(Base):
    __tablename__ = "session_attendances"

    id = Column(Integer, primary_key=True, index=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("training_sessions.id"), nullable=False)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    attended = Column(Boolean, default=False)
    late = Column(Boolean, default=False)
    leave_early = Column(Boolean, default=False)
    checked_at = Column(DateTime)
    remarks = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    enrollment = relationship("Enrollment", back_populates="attendances")
    session = relationship("TrainingSession", back_populates="attendances")
    volunteer = relationship("Volunteer")


class AssessmentCriterion(Base):
    __tablename__ = "assessment_criteria"

    id = Column(Integer, primary_key=True, index=True)
    topic_id = Column(Integer, ForeignKey("assessment_topics.id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    max_score = Column(Float, default=20.0)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    topic = relationship("AssessmentTopic", back_populates="criteria")
    scores = relationship("AssessmentScore", back_populates="criterion", cascade="all, delete-orphan")


class AssessmentQuestion(Base):
    __tablename__ = "assessment_questions"

    id = Column(Integer, primary_key=True, index=True)
    topic_id = Column(Integer, ForeignKey("assessment_topics.id"), nullable=False)
    question = Column(Text, nullable=False)
    reference_answer = Column(Text)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    topic = relationship("AssessmentTopic", back_populates="questions")


class AssessmentScore(Base):
    __tablename__ = "assessment_scores"

    id = Column(Integer, primary_key=True, index=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id"), nullable=False)
    criterion_id = Column(Integer, ForeignKey("assessment_criteria.id"), nullable=False)
    score = Column(Float, default=0.0)
    comments = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    assessment = relationship("Assessment", back_populates="scores")
    criterion = relationship("AssessmentCriterion", back_populates="scores")


class VolunteerCertification(Base):
    __tablename__ = "volunteer_certifications"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    topic_id = Column(Integer, ForeignKey("assessment_topics.id"), nullable=False)
    assessment_id = Column(Integer, ForeignKey("assessments.id"))
    certificate_no = Column(String(50), unique=True)
    issued_date = Column(Date, default=date.today)
    expiry_date = Column(Date)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="certifications")
    topic = relationship("AssessmentTopic", back_populates="certifications")
    assessment = relationship("Assessment")


class Training(Base):
    __tablename__ = "trainings"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    training_date = Column(Date, nullable=False)
    start_time = Column(String(10))
    end_time = Column(String(10))
    location = Column(String(100))
    trainer = Column(String(50))
    content = Column(Text)
    max_participants = Column(Integer, default=30)
    created_at = Column(DateTime, default=datetime.utcnow)

    attendances = relationship("TrainingAttendance", back_populates="training")


class TrainingAttendance(Base):
    __tablename__ = "training_attendances"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"))
    training_id = Column(Integer, ForeignKey("trainings.id"))
    attended = Column(Integer, default=0)
    remarks = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="trainings")
    training = relationship("Training", back_populates="attendances")


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    topic_id = Column(Integer, ForeignKey("assessment_topics.id"))
    training_batch_id = Column(Integer, ForeignKey("training_batches.id"))
    parent_assessment_id = Column(Integer, ForeignKey("assessments.id"))
    assessment_date = Column(Date, nullable=False)
    topic = Column(String(100))
    score = Column(Float)
    result = Column(SAEnum(AssessmentResult), default=AssessmentResult.PENDING)
    is_retake = Column(Boolean, default=False)
    attempt_no = Column(Integer, default=1)
    examiner = Column(String(50))
    comments = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="assessments")
    topic_obj = relationship("AssessmentTopic", back_populates="assessments")
    training_batch = relationship("TrainingBatch", back_populates="assessments")
    parent_assessment = relationship("Assessment", remote_side=[id])
    scores = relationship("AssessmentScore", back_populates="assessment", cascade="all, delete-orphan")
    certification = relationship("VolunteerCertification", back_populates="assessment", uselist=False)


class TimeSlot(Base):
    __tablename__ = "time_slots"

    id = Column(Integer, primary_key=True, index=True)
    slot_date = Column(Date, nullable=False)
    start_time = Column(String(10), nullable=False)
    end_time = Column(String(10), nullable=False)
    topic = Column(String(100))
    location = Column(String(100))
    status = Column(SAEnum(TimeSlotStatus), default=TimeSlotStatus.AVAILABLE)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="time_slots")
    service_record = relationship("ServiceRecord", back_populates="time_slot", uselist=False)


class ServiceRecord(Base):
    __tablename__ = "service_records"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"))
    time_slot_id = Column(Integer, ForeignKey("time_slots.id"))
    service_date = Column(Date, nullable=False)
    service_hours = Column(Float, nullable=False)
    audience_count = Column(Integer, default=0)
    teacher_name = Column(String(50))
    teacher_rating = Column(Integer)
    teacher_comments = Column(Text)
    points_awarded = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="service_records")
    time_slot = relationship("TimeSlot", back_populates="service_record")


class PointsRecord(Base):
    """旧版积分变动记录（服务记录等非兑换流程仍在使用）。

    兑换流程的积分真相以 PointsLedger 不可变流水为准。
    """
    __tablename__ = "points_records"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    points_type = Column(SAEnum(PointsType), nullable=False)
    points_amount = Column(Integer, nullable=False)
    source = Column(SAEnum(PointsSource), nullable=False)
    service_record_id = Column(Integer, ForeignKey("service_records.id"))
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"))
    description = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="points_records")
    service_record = relationship("ServiceRecord")


class PointsLedger(Base):
    """积分流水账：只追加、不修改、不删除。

    对 points_balance / frozen_points 的影响：
      EARN         余额 +amount，冻结 0
      FREEZE       余额 -amount，冻结 +amount（兑换申请预占）
      UNFREEZE     余额 +amount，冻结 -amount（超时/拒绝/取消释放）
      CONSUME      余额 0，冻结 -amount（确认时完成消费）
      REFUND       余额 +amount，冻结 0（部分履约按比例退还）
      MANUAL_ADJUST 余额 += signed_amount（人工补偿，正负皆可），冻结 0

    任何时刻：可用积分 = 流水汇总余额；冻结积分 = 流水汇总冻结额，二者都能逐笔解释。
    """
    __tablename__ = "points_ledgers"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False, index=True)
    ledger_type = Column(SAEnum(PointsLedgerType), nullable=False)
    # FREEZE/UNFREEZE/CONSUME 按预占原值记正数；EARN 记获得数；MANUAL_ADJUST 记带符号数
    amount = Column(Integer, nullable=False)
    reason = Column(SAEnum(ExchangeReleaseReason))
    ref_type = Column(String(30), index=True)   # exchange / entitlement / compensation / opening
    ref_id = Column(Integer, index=True)
    # 兑换流程生成的流水带幂等键并唯一，同一阶段重放绝不会重复入账
    idempotency_key = Column(String(80), unique=True)
    description = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_points_ledger_idempotency_key"),
    )

    volunteer = relationship("Volunteer", back_populates="points_ledgers")
    exchange = relationship(
        "BenefitExchange",
        primaryjoin="and_(foreign(PointsLedger.ref_id)==BenefitExchange.id, "
                    "PointsLedger.ref_type=='exchange')",
        viewonly=True,
    )


class Benefit(Base):
    __tablename__ = "benefits"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    benefit_type = Column(SAEnum(BenefitType), nullable=False)
    description = Column(Text)
    points_cost = Column(Integer, nullable=False)
    # 可售库存（总库存）。已承诺数量由预占/确认中的兑换流水汇总，不另存可变计数
    stock = Column(Integer, default=0)
    # 实物权益允许部分履约；优先时段券确认后以资格形式发放
    allow_partial_fulfillment = Column(Boolean, default=False)
    # 预占超时秒数：超过该时长未确认的预占可由超时回收任务释放
    reserve_timeout_seconds = Column(Integer, default=900)
    is_active = Column(Boolean, default=True)
    image_url = Column(String(500))
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    exchanges = relationship("BenefitExchange", back_populates="benefit")


class IdempotentRequest(Base):
    """幂等请求登记：同一客户端请求键 + 资源只处理一次。

    载荷指纹变化时返回 409 冲突，绝不重复执行。
    """
    __tablename__ = "idempotent_requests"

    id = Column(Integer, primary_key=True, index=True)
    request_key = Column(String(80), nullable=False)
    # 幂等作用域，如 exchange:apply / exchange:confirm / entitlement:consume
    scope = Column(String(40), nullable=False)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"))
    payload_hash = Column(String(64), nullable=False)
    # 首次成功处理后落定的资源（如兑换单 ID）
    resource_type = Column(String(30))
    resource_id = Column(Integer)
    response_status = Column(Integer)
    response_body = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("scope", "request_key", name="uq_idempotent_scope_key"),
    )


class BenefitExchange(Base):
    __tablename__ = "benefit_exchanges"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    benefit_id = Column(Integer, ForeignKey("benefits.id"), nullable=False)
    # 预占的积分总额与数量（确认/履约阶段不可变）
    points_spent = Column(Integer, nullable=False)
    points_cost = Column(Integer, nullable=False)
    quantity = Column(Integer, default=1)
    status = Column(SAEnum(ExchangeStatus), default=ExchangeStatus.RESERVED, nullable=False, index=True)
    # 已实际履约数量（实物可部分履约）；优先时段券等于已发资格数
    fulfilled_quantity = Column(Integer, default=0, nullable=False)
    # 已消费积分（FREEZE -> CONSUME 累计）；剩余预占积分 = points_spent - points_consumed - points_refunded
    points_consumed = Column(Integer, default=0, nullable=False)
    points_refunded = Column(Integer, default=0, nullable=False)
    # 状态终结原因（超时/拒绝/取消/部分库存不足/人工补偿）
    close_reason = Column(SAEnum(ExchangeReleaseReason))
    reserve_expires_at = Column(DateTime, index=True)
    confirmed_at = Column(DateTime)
    delivery_info = Column(Text)
    fulfilled_at = Column(DateTime)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="benefit_exchanges")
    benefit = relationship("Benefit", back_populates="exchanges")
    ledgers = relationship(
        "PointsLedger",
        primaryjoin="and_(foreign(PointsLedger.ref_id)==BenefitExchange.id, "
                    "PointsLedger.ref_type=='exchange')",
        viewonly=True,
    )
    inventory_records = relationship("InventoryRecord", back_populates="exchange")
    entitlements = relationship("PriorityEntitlement", back_populates="exchange")
    compensations = relationship("ManualCompensation", back_populates="exchange")


class InventoryRecord(Base):
    """库存流水：只追加。每次预占/确认/释放/部分履约都留痕。

    committed_delta 对"已承诺数量"的贡献：
      RESERVE  +qty   CONFIRM 0（预占转已售，承诺在履约时才减少）
      FULFILL  -qty   RELEASE  -qty
    可售库存与已承诺数量都能由流水 + 商品初始库存逐笔解释。
    """
    __tablename__ = "inventory_records"

    id = Column(Integer, primary_key=True, index=True)
    benefit_id = Column(Integer, ForeignKey("benefits.id"), nullable=False, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"))
    action = Column(String(20), nullable=False)  # RESERVE/RELEASE/FULFILL/ADMIN_ADJUST
    # quantity：对"可售库存"的带符号增量（RESERVE -q / RELEASE +q / FULFILL 0 / ADMIN_ADJUST 带符号）
    quantity = Column(Integer, nullable=False)
    # committed_delta：对"已承诺数量"的带符号增量（RESERVE +q / RELEASE -q / FULFILL -q / ADMIN 0）
    committed_delta = Column(Integer, nullable=False, default=0)
    reason = Column(SAEnum(ExchangeReleaseReason))
    created_at = Column(DateTime, default=datetime.utcnow)

    exchange = relationship("BenefitExchange", back_populates="inventory_records")


class PriorityEntitlement(Base):
    """优先时段券资格：确认兑换时发放（ISSUED），真正认领时段时才消费（USED）。"""
    __tablename__ = "priority_entitlements"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False, index=True)
    benefit_id = Column(Integer, ForeignKey("benefits.id"), nullable=False)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"), nullable=False)
    time_slot_id = Column(Integer, ForeignKey("time_slots.id"))
    status = Column(SAEnum(EntitlementStatus), default=EntitlementStatus.ISSUED, nullable=False, index=True)
    code = Column(String(40), unique=True, nullable=False)
    used_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="entitlements")
    exchange = relationship("BenefitExchange", back_populates="entitlements")
    time_slot = relationship("TimeSlot")


class ManualCompensation(Base):
    """后台人工更正：只能追加补偿记录，禁止回改原始兑换单与流水。"""
    __tablename__ = "manual_compensations"

    id = Column(Integer, primary_key=True, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"))
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    benefit_id = Column(Integer, ForeignKey("benefits.id"))
    points_delta = Column(Integer, default=0)       # 带符号：补还为正，扣回为负
    inventory_delta = Column(Integer, default=0)    # 带符号：补库存为正
    reason = Column(String(200), nullable=False)
    operator = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    exchange = relationship("BenefitExchange", back_populates="compensations")


class StarCertificate(Base):
    __tablename__ = "star_certificates"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    star_level_id = Column(Integer, ForeignKey("star_levels.id"), nullable=False)
    certificate_no = Column(String(50), unique=True, nullable=False)
    issued_date = Column(Date, default=date.today)
    total_hours = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="star_certificates")
    star_level = relationship("StarLevel")
