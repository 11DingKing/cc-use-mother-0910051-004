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
    # 预占不是独立余额：冻结/解冻/结算均成对记录在同一张积分流水表，
    # 可用积分 = 累计获得 - 累计消耗 - 冻结中的支出，任何时候都能用流水还原。
    FREEZE = "冻结"
    UNFREEZE = "解冻"
    SETTLE_SPEND = "结算支出"
    SETTLE_REFUND = "结算退回"


class PointsSource(str, enum.Enum):
    SERVICE_COMPLETION = "完成讲解"
    TEACHER_RATING = "老师好评"
    EXCHANGE_BADGE = "兑换徽章"
    EXCHANGE_PRIORITY_SLOT = "兑换优先时段"
    EXCHANGE_RESERVE = "兑换预占"
    EXCHANGE_RELEASE = "兑换释放"
    EXCHANGE_SETTLE = "兑换结算"
    MANUAL_COMPENSATION = "人工补偿"
    PRIORITY_USE = "优先券核销"
    OTHER = "其他权益"


class BenefitType(str, enum.Enum):
    BADGE = "纪念徽章"
    PRIORITY_SLOT = "优先认领时段"
    OTHER = "其他权益"


class ExchangeStatus(str, enum.Enum):
    # 申请即预占：RESERVED 表示积分与库存都已原子锁定；确认后进入 CONFIRMED。
    RESERVED = "已预占"
    PENDING = "待处理"              # 兼容旧数据
    CONFIRMED = "已确认待履约"
    PARTIALLY_FULFILLED = "部分履约"
    COMPLETED = "已完成"
    REJECTED = "已拒绝"
    CANCELLED = "已取消"
    TIMEOUT_CANCELLED = "超时取消"


class ReleaseReason(str, enum.Enum):
    TIMEOUT = "超时释放"
    REJECTED = "后台拒绝"
    USER_CANCEL = "家长取消"
    PARTIAL_RELEASE = "部分履约释放"


class ExchangeEventType(str, enum.Enum):
    RESERVE = "申请预占"
    CONFIRM = "后台确认"
    FULFILL = "实物履约"
    PARTIAL_FULFILL = "部分履约"
    REJECT = "拒绝"
    CANCEL = "取消"
    TIMEOUT = "超时取消"
    COMPENSATE = "人工补偿"
    COUPON_USE = "优先券核销"
    COUPON_RETURN = "优先券退回"


class CompensationType(str, enum.Enum):
    POINTS_REFUND = "补退积分"
    POINTS_DEDUCT = "补扣积分"
    STOCK_RETURN = "补回库存"
    STOCK_DEDUCT = "补扣库存"


class CouponStatus(str, enum.Enum):
    # 优先时段券：兑换确认只是发放资格，真正使用（核销时段）才消费资格。
    ISSUED = "已发放"
    USED = "已使用"
    RETURNED = "已退回"
    EXPIRED = "已过期"


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
    points_balance = Column(Integer, default=0)
    # 已预占但尚未结算/释放的积分：可用积分 = points_balance - points_frozen
    points_frozen = Column(Integer, default=0, nullable=False)
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
    benefit_exchanges = relationship("BenefitExchange", back_populates="volunteer")
    star_certificates = relationship("StarCertificate", back_populates="volunteer")


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
    __tablename__ = "points_records"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    points_type = Column(SAEnum(PointsType), nullable=False)
    points_amount = Column(Integer, nullable=False)
    source = Column(SAEnum(PointsSource), nullable=False)
    service_record_id = Column(Integer, ForeignKey("service_records.id"))
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"))
    # 每一条冻结/解冻/结算流水都挂到具体兑换事件上，保证逐笔可解释。
    exchange_event_id = Column(Integer, ForeignKey("exchange_events.id"))
    description = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="points_records")
    service_record = relationship("ServiceRecord")
    exchange = relationship("BenefitExchange", back_populates="points_records")
    exchange_event = relationship("ExchangeEvent", back_populates="points_records")


class Benefit(Base):
    __tablename__ = "benefits"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    benefit_type = Column(SAEnum(BenefitType), nullable=False)
    description = Column(Text)
    points_cost = Column(Integer, nullable=False)
    stock = Column(Integer, default=0)
    # 已售罄承诺数量（已预占 + 已履约，未因取消/拒绝释放）。
    # 库存守恒：stock + committed_quantity = 初始可售量（人工补偿以流水另行解释差异）。
    committed_quantity = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True)
    image_url = Column(String(500))
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    exchanges = relationship("BenefitExchange", back_populates="benefit")


class BenefitExchange(Base):
    __tablename__ = "benefit_exchanges"

    id = Column(Integer, primary_key=True, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False)
    benefit_id = Column(Integer, ForeignKey("benefits.id"), nullable=False)
    # 申请时按当时单价锁定的积分总额，后续确认/取消均以此为准，单价漂移不影响在途兑换。
    points_spent = Column(Integer, nullable=False)
    status = Column(SAEnum(ExchangeStatus), default=ExchangeStatus.RESERVED)
    quantity = Column(Integer, default=1)
    # 幂等键：家长端重试携带同一 request_no 必须返回同一笔兑换；
    # request_payload_hash 记录申请载荷指纹，载荷变化识别为 409 冲突。
    request_no = Column(String(64), unique=True, index=True)
    request_payload_hash = Column(String(64))
    # 数量账：reserved=已预占未履约，fulfilled=已履约，released=已释放，三者守恒 quantity = reserved + fulfilled + released
    reserved_quantity = Column(Integer, default=0, nullable=False)
    fulfilled_quantity = Column(Integer, default=0, nullable=False)
    released_quantity = Column(Integer, default=0, nullable=False)
    # 积分账：frozen=冻结中，settled=已结算毛额；
    # released=预占后整单释放回可用（取消/拒绝/超时）；refunded=已结算后退回（部分履约/补偿）
    # points_spent = frozen + settled + released
    points_frozen = Column(Integer, default=0, nullable=False)
    points_settled = Column(Integer, default=0, nullable=False)
    points_released = Column(Integer, default=0, nullable=False)
    points_refunded = Column(Integer, default=0, nullable=False)
    expires_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    delivery_info = Column(Text)
    fulfilled_at = Column(DateTime)
    cancel_reason = Column(SAEnum(ReleaseReason))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    volunteer = relationship("Volunteer", back_populates="benefit_exchanges")
    benefit = relationship("Benefit", back_populates="exchanges")
    points_records = relationship("PointsRecord", back_populates="exchange")
    events = relationship("ExchangeEvent", back_populates="exchange",
                          cascade="all, delete-orphan", order_by="ExchangeEvent.id")
    fulfillments = relationship("FulfillmentItem", back_populates="exchange",
                                cascade="all, delete-orphan", order_by="FulfillmentItem.id")
    coupons = relationship("PriorityCoupon", back_populates="exchange",
                           cascade="all, delete-orphan")
    compensations = relationship("ManualCompensation", back_populates="exchange",
                                 cascade="all, delete-orphan", order_by="ManualCompensation.id")


class ExchangeEvent(Base):
    """兑换全生命周期事件流：每个状态迁移与资源变动追加一条，永不修改删除。"""
    __tablename__ = "exchange_events"

    id = Column(Integer, primary_key=True, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"), nullable=False, index=True)
    event_type = Column(SAEnum(ExchangeEventType), nullable=False)
    from_status = Column(SAEnum(ExchangeStatus))
    to_status = Column(SAEnum(ExchangeStatus))
    quantity = Column(Integer, default=0)
    points_amount = Column(Integer, default=0)
    reason = Column(SAEnum(ReleaseReason))
    detail = Column(Text)
    operator = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    exchange = relationship("BenefitExchange", back_populates="events")
    points_records = relationship("PointsRecord", back_populates="exchange_event")


class FulfillmentItem(Base):
    """实物权益逐批复品记录，支持部分履约。"""
    __tablename__ = "fulfillment_items"

    id = Column(Integer, primary_key=True, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"), nullable=False, index=True)
    # 幂等键：后台重复提交同一批复品请求不重复出库
    request_no = Column(String(64), unique=True, index=True)
    quantity = Column(Integer, nullable=False)
    delivery_info = Column(Text)
    operator = Column(String(50))
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    exchange = relationship("BenefitExchange", back_populates="fulfillments")


class StockChangeType(str, enum.Enum):
    INITIAL = "初始入库"
    RESERVE = "预占出库"
    RELEASE = "释放回库"
    FULFILL = "履约出库"
    COMPENSATE_IN = "补偿回库"
    COMPENSATE_OUT = "补偿扣库"


class StockLedger(Base):
    """库存流水：每一笔 stock/committed 变动都追加记录，可售库存与已承诺数量逐笔可解释。"""
    __tablename__ = "stock_ledger"

    id = Column(Integer, primary_key=True, index=True)
    benefit_id = Column(Integer, ForeignKey("benefits.id"), nullable=False, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"), index=True)
    change_type = Column(SAEnum(StockChangeType), nullable=False)
    quantity = Column(Integer, nullable=False)            # 可售库存变动（正入库/负出库）
    committed_delta = Column(Integer, nullable=False, default=0)  # 已承诺数量变动
    stock_after = Column(Integer, nullable=False)
    committed_after = Column(Integer, nullable=False)
    event_id = Column(Integer, ForeignKey("exchange_events.id"))
    operator = Column(String(50))
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    benefit = relationship("Benefit")
    exchange = relationship("BenefitExchange")


class PriorityCoupon(Base):
    """优先认领时段券：确认发放资格，核销（绑定讲解时段）时才真正消费。"""
    __tablename__ = "priority_coupons"

    id = Column(Integer, primary_key=True, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"), nullable=False, index=True)
    volunteer_id = Column(Integer, ForeignKey("volunteers.id"), nullable=False, index=True)
    benefit_id = Column(Integer, ForeignKey("benefits.id"), nullable=False)
    coupon_no = Column(String(64), unique=True, nullable=False, index=True)
    status = Column(SAEnum(CouponStatus), default=CouponStatus.ISSUED, nullable=False)
    time_slot_id = Column(Integer, ForeignKey("time_slots.id"))
    used_at = Column(DateTime)
    issued_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    exchange = relationship("BenefitExchange", back_populates="coupons")
    volunteer = relationship("Volunteer")
    benefit = relationship("Benefit")
    time_slot = relationship("TimeSlot", foreign_keys=[time_slot_id])


class ManualCompensation(Base):
    """后台人工更正只能追加补偿记录，永不改写既有流水与状态字段。"""
    __tablename__ = "manual_compensations"
    __table_args__ = (
        UniqueConstraint("exchange_id", "request_no", name="uq_compensation_request_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    exchange_id = Column(Integer, ForeignKey("benefit_exchanges.id"), nullable=False, index=True)
    request_no = Column(String(64))
    compensation_type = Column(SAEnum(CompensationType), nullable=False)
    amount = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
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
