from app.ingestion.migration_planner import (
    build_migration_plan,
    render_pom_migration_instruction,
    render_resource_migration_instruction,
    select_openrewrite_recipes,
)
from app.models.manifest import MavenDependency, PomManifest


def test_dynamic_migration_plan_for_arbitrary_boot_2_project():
    pom = PomManifest(
        spring_boot_version="2.5.8",
        java_version="11",
        properties={
            "java.version": "11",
            "thymeleaf.version": "3.0.14.RELEASE",
        },
        repositories=["https://repo.spring.io/milestone"],
        dependencies=[
            MavenDependency(
                group_id="org.springframework.boot",
                artifact_id="spring-boot-starter-data-jpa",
            ),
            MavenDependency(
                group_id="org.thymeleaf.extras",
                artifact_id="thymeleaf-extras-springsecurity5",
            ),
            MavenDependency(
                group_id="org.postgresql",
                artifact_id="postgresql",
            ),
        ],
    )

    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")

    assert plan.source_spring_boot == "2.5.8"
    assert plan.target_spring_boot == "3.2.5"
    assert plan.target_java == "17"
    assert "thymeleaf.version" in plan.properties_to_remove
    assert plan.properties_to_update["java.version"] == "17"
    assert plan.repositories_to_remove == ["https://repo.spring.io/milestone"]
    assert "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_2" in plan.openrewrite_recipes

    actions = {d.artifact_id: d for d in plan.dependencies}
    assert actions["spring-boot-starter-data-jpa"].target_strategy == "use_boot_bom"
    assert actions["thymeleaf-extras-springsecurity5"].target_artifact_id == "thymeleaf-extras-springsecurity6"
    assert actions["postgresql"].target_strategy == "keep_or_verify"

    instruction = render_pom_migration_instruction(plan)
    assert "2.5.8" in instruction
    assert "3.2.5" in instruction
    assert "selen" not in instruction.lower()  # No hardcoded JPetStore artifacts!


def test_dynamic_migration_plan_upgrades_selenide_when_present():
    pom = PomManifest(
        spring_boot_version="2.7.0",
        java_version="1.8",
        properties={"selenide.version": "6.6.1", "selenium.version": "4.1.0"},
        dependencies=[
            MavenDependency(group_id="com.codeborne", artifact_id="selenide", version="6.6.1"),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    assert plan.properties_to_update["selenide.version"] == "7.2.3"
    assert "selenium.version" in plan.properties_to_remove
    assert plan.dependencies[0].target_version == "7.2.3"


def test_select_openrewrite_recipes_maps_minor_versions():
    assert "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0" in select_openrewrite_recipes("3.0.2")
    assert "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_3" in select_openrewrite_recipes("3.3.1")


def test_resource_migration_instruction_handles_yaml_and_thymeleaf():
    yml_instr = render_resource_migration_instruction("application.yml", "spring:\n  redis:\n    host: localhost\n")
    assert "spring.data.redis" in yml_instr

    html_instr = render_resource_migration_instruction("layout.html", "<body th:with=\"x=${#request.getAttribute('javax.servlet.error.status_code')}\">")
    assert "#request" in html_instr
    assert "jakarta.*" in html_instr


def test_migration_plan_upgrades_mysql_springfox_and_lombok():
    pom = PomManifest(
        spring_boot_version="2.5.4",
        java_version="1.8",
        properties={"java.version": "1.8"},
        dependencies=[
            MavenDependency(group_id="mysql", artifact_id="mysql-connector-java"),
            MavenDependency(group_id="org.projectlombok", artifact_id="lombok", version="1.18.20"),
            MavenDependency(group_id="io.springfox", artifact_id="springfox-swagger2", version="2.9.2"),
            MavenDependency(group_id="io.springfox", artifact_id="springfox-swagger-ui", version="2.9.2"),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    actions = {(d.group_id, d.artifact_id): d for d in plan.dependencies}

    mysql = actions[("mysql", "mysql-connector-java")]
    assert mysql.target_strategy == "upgrade_artifact"
    assert mysql.target_group_id == "com.mysql"
    assert mysql.target_artifact_id == "mysql-connector-j"
    assert mysql.remove_version is True

    lombok = actions[("org.projectlombok", "lombok")]
    assert lombok.target_strategy == "upgrade_version"
    assert lombok.target_version == "1.18.32"

    swag2 = actions[("io.springfox", "springfox-swagger2")]
    assert swag2.target_strategy == "upgrade_artifact"
    assert swag2.target_group_id == "org.springdoc"
    assert swag2.target_artifact_id == "springdoc-openapi-starter-webmvc-ui"
    assert swag2.target_version == "2.5.0"

    swag_ui = actions[("io.springfox", "springfox-swagger-ui")]
    assert swag_ui.target_strategy == "remove_artifact"


def test_migration_plan_handles_bom_removed_jakarta_hibernate_and_logback():
    pom = PomManifest(
        spring_boot_version="2.7.18",
        java_version="1.8",
        properties={"java.version": "1.8"},
        dependencies=[
            MavenDependency(group_id="javax.servlet", artifact_id="javax.servlet-api"),
            MavenDependency(group_id="javax.servlet", artifact_id="jstl"),
            MavenDependency(group_id="javax.servlet.jsp", artifact_id="jsp-api", version="2.1"),
            MavenDependency(group_id="org.hibernate", artifact_id="hibernate-entitymanager"),
            MavenDependency(group_id="org.hibernate", artifact_id="hibernate-core"),
            MavenDependency(group_id="ch.qos.logback", artifact_id="logback-classic", version="1.2.3"),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    actions = {(d.group_id, d.artifact_id): d for d in plan.dependencies}

    servlet = actions[("javax.servlet", "javax.servlet-api")]
    assert servlet.target_group_id == "jakarta.servlet"
    assert servlet.target_artifact_id == "jakarta.servlet-api"
    assert servlet.remove_version is True

    jstl = actions[("javax.servlet", "jstl")]
    assert jstl.target_group_id == "jakarta.servlet.jsp.jstl"
    assert jstl.target_artifact_id == "jakarta.servlet.jsp.jstl-api"
    assert jstl.remove_version is True

    jsp = actions[("javax.servlet.jsp", "jsp-api")]
    assert jsp.target_group_id == "jakarta.servlet.jsp"
    assert jsp.target_artifact_id == "jakarta.servlet.jsp-api"
    assert jsp.target_version == "3.1.1"

    assert actions[("org.hibernate", "hibernate-entitymanager")].target_strategy == "remove_artifact"
    hib_core = actions[("org.hibernate", "hibernate-core")]
    assert hib_core.target_group_id == "org.hibernate.orm"
    assert hib_core.target_artifact_id == "hibernate-core"
    assert hib_core.remove_version is True

    logback = actions[("ch.qos.logback", "logback-classic")]
    assert logback.target_strategy == "use_boot_bom"
    assert logback.remove_version is True


def test_migration_plan_upgrades_shiro_urlrewrite_sitemesh_druid_and_jackson():
    pom = PomManifest(
        spring_boot_version="2.7.18",
        java_version="1.8",
        properties={"java.version": "1.8"},
        dependencies=[
            MavenDependency(group_id="org.apache.shiro", artifact_id="shiro-all", version="1.10.0"),
            MavenDependency(group_id="org.tuckey", artifact_id="urlrewritefilter", version="4.0.3"),
            MavenDependency(group_id="org.sitemesh", artifact_id="sitemesh", version="3.0.1"),
            MavenDependency(group_id="com.alibaba", artifact_id="druid-spring-boot-starter", version="1.2.16"),
            MavenDependency(group_id="com.fasterxml.jackson.core", artifact_id="jackson-databind", version="2.13.4.1"),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    actions = {(d.group_id, d.artifact_id): d for d in plan.dependencies}

    shiro = actions[("org.apache.shiro", "shiro-all")]
    assert shiro.target_strategy == "upgrade_artifact"
    assert shiro.target_artifact_id == "shiro-core"
    assert shiro.target_version == "2.0.2"
    assert shiro.target_classifier == "jakarta"
    extra_ids = {e.artifact_id: e for e in shiro.additional_artifacts}
    assert set(extra_ids) == {"shiro-web", "shiro-spring"}
    assert extra_ids["shiro-web"].classifier == "jakarta"
    assert ("org.apache.shiro", "*") in extra_ids["shiro-web"].exclusions
    assert ("org.apache.shiro", "*") in extra_ids["shiro-spring"].exclusions

    assert actions[("org.tuckey", "urlrewritefilter")].target_version == "5.1.3"
    assert actions[("org.sitemesh", "sitemesh")].target_version == "3.2.1"

    druid = actions[("com.alibaba", "druid-spring-boot-starter")]
    assert druid.target_artifact_id == "druid-spring-boot-3-starter"
    assert druid.target_version == "1.2.23"

    jackson = actions[("com.fasterxml.jackson.core", "jackson-databind")]
    assert jackson.target_strategy == "use_boot_bom"
    assert jackson.remove_version is True

    instruction = render_pom_migration_instruction(plan)
    assert "shiro-core:2.0.2:jakarta" in instruction
    assert "shiro-web:2.0.2:jakarta" in instruction


def test_migration_plan_upgrades_shiro_boot_web_starter_without_duplicating_core():
    pom = PomManifest(
        spring_boot_version="2.7.10",
        java_version="11",
        dependencies=[
            MavenDependency(group_id="org.apache.shiro", artifact_id="shiro-core", version="1.11.0"),
            MavenDependency(
                group_id="org.apache.shiro",
                artifact_id="shiro-spring-boot-web-starter",
                version="1.11.0",
            ),
        ],
    )
    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    actions = {(d.group_id, d.artifact_id): d for d in plan.dependencies}

    core = actions[("org.apache.shiro", "shiro-core")]
    assert core.target_version == "2.0.2"
    assert core.target_classifier == "jakarta"
    assert core.exclusions == []

    starter = actions[("org.apache.shiro", "shiro-spring-boot-web-starter")]
    assert starter.target_classifier == "jakarta"
    assert ("org.apache.shiro", "*") in starter.exclusions
    extra_artifacts = [e.artifact_id for e in starter.additional_artifacts]
    assert "shiro-core" not in extra_artifacts
    assert "shiro-web" in extra_artifacts
    assert "shiro-spring" in extra_artifacts


def test_migration_plan_pins_bom_dropped_apache_httpcomponents_v4_versions():
    pom = PomManifest(
        spring_boot_version="2.7.18",
        java_version="1.8",
        dependencies=[
            MavenDependency(group_id="org.apache.httpcomponents", artifact_id="httpclient"),
            MavenDependency(group_id="org.apache.httpcomponents", artifact_id="httpclient-cache"),
            MavenDependency(group_id="org.apache.httpcomponents", artifact_id="httpmime"),
            MavenDependency(
                group_id="org.apache.httpcomponents",
                artifact_id="httpcore",
                version="4.4.15",
            ),
            MavenDependency(
                group_id="org.elasticsearch.client",
                artifact_id="elasticsearch-rest-high-level-client",
                version="7.17.10",
            ),
        ],
    )

    plan = build_migration_plan(pom, target_spring_boot="3.2.5", target_java="17")
    actions = {(d.group_id, d.artifact_id): d for d in plan.dependencies}

    assert actions[("org.apache.httpcomponents", "httpclient")].target_strategy == "upgrade_version"
    assert actions[("org.apache.httpcomponents", "httpclient")].target_version == "4.5.14"
    assert actions[("org.apache.httpcomponents", "httpclient-cache")].target_version == "4.5.14"
    assert actions[("org.apache.httpcomponents", "httpmime")].target_version == "4.5.14"
    # Explicitly versioned httpcore stays untouched
    assert actions[("org.apache.httpcomponents", "httpcore")].target_strategy == "keep_or_verify"

