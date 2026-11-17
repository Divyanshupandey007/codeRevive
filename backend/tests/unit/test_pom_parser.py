from pathlib import Path
import pytest

from app.ingestion.pom_parser import PomParser


def test_pom_parser_extracts_metadata_properties_and_repositories(tmp_path: Path):
    pom = tmp_path / "pom.xml"
    pom.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>2.5.8</version>
    </parent>
    <groupId>com.acme</groupId>
    <artifactId>orders</artifactId>
    <version>1.0.0</version>
    <properties>
        <java.version>11</java.version>
        <mybatis.version>2.2.2</mybatis.version>
    </properties>
    <repositories>
        <repository>
            <id>spring-milestones</id>
            <url>https://repo.spring.io/milestone</url>
        </repository>
    </repositories>
    <dependencies>
        <dependency>
            <groupId>org.mybatis.spring.boot</groupId>
            <artifactId>mybatis-spring-boot-starter</artifactId>
            <version>${mybatis.version}</version>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-test</artifactId>
            <scope>test</scope>
        </dependency>
    </dependencies>
</project>
""",
        encoding="utf-8",
    )

    manifest = PomParser(pom).parse()
    assert manifest.spring_boot_version == "2.5.8"
    assert manifest.java_version == "11"
    assert manifest.is_multi_module is False
    assert manifest.repositories == ["https://repo.spring.io/milestone"]
    assert manifest.dependencies[0].version == "2.2.2"
    assert manifest.dependencies[1].scope == "test"


def test_pom_parser_detects_multi_module(tmp_path: Path):
    pom = tmp_path / "pom.xml"
    pom.write_text(
        """<project>
    <modelVersion>4.0.0</modelVersion>
    <packaging>pom</packaging>
    <modules>
        <module>user-service</module>
        <module>order-service</module>
    </modules>
</project>""",
        encoding="utf-8",
    )

    manifest = PomParser(pom).parse()
    assert manifest.is_multi_module is True
    assert manifest.modules == ["user-service", "order-service"]


def test_pom_parser_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        PomParser(tmp_path / "missing.xml")
