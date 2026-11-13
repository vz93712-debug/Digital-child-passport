"""Матрица доступа: ключевые сценарии из кейса."""
from datetime import date

from passport.access import (ChildContext, Consent, Participation, Viewer, VisibilityRule,
                             can_set_visibility, decide)

CAMP, PARTNER, OTHER = "issuer-camp", "issuer-partner", "issuer-other"
TODAY = date(2027, 6, 10)


def ctx(age="10-13", consents=(), visibility=()):
    return ChildContext(
        child_id="masha", age_band=age,
        participations=[Participation("shift.robots-ai", CAMP, date(2027, 6, 2), date(2027, 6, 22))],
        consents=list(consents), visibility=list(visibility))


PARENT = Viewer("guardian", "kc-parent", guardian_of=frozenset({"masha"}))
STRANGER_PARENT = Viewer("guardian", "kc-other-parent", guardian_of=frozenset({"petya"}))
COUNSELOR = Viewer("counselor", "kc-counselor", program_ids=frozenset({"shift.robots-ai"}))
OTHER_COUNSELOR = Viewer("counselor", "kc-c2", program_ids=frozenset({"shift.media"}))
TEACHER = Viewer("teacher", "kc-teacher", program_ids=frozenset({"course.python-start"}))
ADMIN = Viewer("camp_admin", "kc-admin", issuer_id=CAMP)
PARTNER_V = Viewer("partner", "client-partner", issuer_id=PARTNER)
CHILD = Viewer("child", "kc-masha", child_id="masha")
SHARE = Consent("share_partner", frozenset({"achievements"}), PARTNER)


def test_parent_sees_own_child_only():
    assert decide(PARENT, ctx(), "achievements").allowed
    assert not decide(STRANGER_PARENT, ctx(), "achievements").allowed


def test_counselor_scoped_to_own_shift_and_period():
    assert decide(COUNSELOR, ctx(), "skills", today=TODAY).allowed
    assert not decide(OTHER_COUNSELOR, ctx(), "skills", today=TODAY).allowed
    # через 30 дней после смены доступ закрывается
    assert decide(COUNSELOR, ctx(), "skills", today=date(2027, 7, 20)).allowed
    assert not decide(COUNSELOR, ctx(), "skills", today=date(2027, 7, 23)).allowed


def test_counselor_writes_observations_parent_cannot():
    assert decide(COUNSELOR, ctx(), "observations", "write", today=TODAY).allowed
    assert not decide(PARENT, ctx(), "observations", "write").allowed


def test_teacher_without_course_sees_nothing():
    assert not decide(TEACHER, ctx(), "skills", today=TODAY).allowed


def test_partner_needs_consent_for_exact_category():
    assert not decide(PARTNER_V, ctx(), "achievements").allowed
    c = ctx(consents=[SHARE])
    assert decide(PARTNER_V, c, "achievements").allowed
    assert not decide(PARTNER_V, c, "skills").allowed          # категория не выбрана родителем
    assert not decide(Viewer("partner", "x", issuer_id=OTHER), c, "achievements").allowed
    assert not decide(PARTNER_V, c, "observations").allowed     # партнёру — никогда


def test_activity_requires_purpose_consent():
    assert not decide(PARENT, ctx(), "activity").allowed
    assert decide(PARENT, ctx(consents=[Consent("activity")]), "activity").allowed
    assert not decide(ADMIN, ctx(consents=[Consent("activity")]), "activity").allowed


def test_young_child_does_not_read_observations():
    assert not decide(Viewer("child", "k", child_id="masha"), ctx(age="4-9"), "observations").allowed
    assert decide(CHILD, ctx(age="10-13"), "observations").allowed


def test_hidden_item_hidden_from_partner_but_not_from_child():
    rule = VisibilityRule("achievement", "a1", frozenset({"partner"}), "child")
    c = ctx(consents=[SHARE], visibility=[rule])
    assert not decide(PARTNER_V, c, "achievements", item_id="a1").allowed
    assert decide(PARTNER_V, c, "achievements", item_id="a2").allowed
    assert decide(CHILD, c, "achievements", item_id="a1").allowed


def test_teen_hides_from_parent_parent_sees_placeholder():
    rule = VisibilityRule("achievement", "a1", frozenset({"guardian"}), "child")
    d = decide(PARENT, ctx(age="14-17", visibility=[rule]), "achievements", item_id="a1")
    assert not d.allowed and d.placeholder


def test_author_always_sees_own_observation():
    rule = VisibilityRule("observation", "o1", frozenset({"counselor"}), "child")
    c = ctx(visibility=[rule])
    assert decide(COUNSELOR, c, "observations", item_id="o1", item_author_sub="kc-counselor", today=TODAY).allowed
    assert not decide(COUNSELOR, c, "observations", item_id="o1", item_author_sub="kc-someone", today=TODAY).allowed


def test_hide_rights_depend_on_age():
    assert not can_set_visibility("4-9", "child", frozenset({"partner"}))[0]
    assert can_set_visibility("10-13", "child", frozenset({"partner", "export"}))[0]
    assert not can_set_visibility("10-13", "child", frozenset({"guardian"}))[0]
    assert can_set_visibility("14-17", "child", frozenset({"guardian"}))[0]
    assert not can_set_visibility("14-17", "guardian", frozenset({"guardian"}))[0]
