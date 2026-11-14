from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import List, TYPE_CHECKING

backend_root = Path(__file__).resolve().parents[2]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

if TYPE_CHECKING:
    from app.models.manifest import JavaFileManifest

@dataclass
class MigrationRule:
    """One category of Spring Boot 2→3 breaking change.

    The predicate receives a JavaFileManifest (full AST info: imports, classes, etc.).
    """
    id:str
    description:str
    predicate:Callable[["JavaFileManifest"],bool]
    migration_instruction:str
    commit_summary:str

SPRING_BOOT_3_RULES:List[MigrationRule]=[
    MigrationRule(
        id="javax-to-jakarta",
        description="javax.* namespace -> jakarta.*",
        predicate=lambda m:bool(m.javax_imports),
        migration_instruction=(
            "Migrate all javax.* imports and annotations in this file to "
            "their jakarta.* equivalents (javax.validation→jakarta.validation, "
            "javax.servlet→jakarta.servlet, javax.persistence→jakarta.persistence, "
            "etc.). Do not change anything else."
        ),
        commit_summary="javax.* -> jakarta.*"
    ),
    MigrationRule(
        id="thymeleaf-spring5-to-spring6",
        description="org.thymeleaf.spring5 -> org.thymeleaf.spring6",
        predicate=lambda m:any(
            i.startswith("org.thymeleaf.spring5") for i in m.imports
        ),
        migration_instruction=(
            "Replace all org.thymeleaf.spring5.* imports with their "
            "org.thymeleaf.spring6.* equivalents. The class names are identical; "
            "only the package segment changes (spring5 → spring6). "
            "Do not change anything else."
        ),
        commit_summary="thymeleaf spring5 -> spring6"
    ),
    MigrationRule(
        id="security-configurer-adapter-removal",
        description="WebSecurityConfigurerAdapter removal",
        predicate=lambda m:(
            any("WebSecurityConfigurerAdapter" in i for i in m.imports)
            or any(
                "WebSecurityConfigurerAdapter" in ext
                for c in m.classes for ext in c.extends
            )
        ),
        migration_instruction=(
            "Spring Security 6.0 removed WebSecurityConfigurerAdapter. Refactor "
            "this class to use the component-based security configuration:\n"
            "1. Remove 'extends WebSecurityConfigurerAdapter' and its import.\n"
            "2. Convert configure(HttpSecurity http) into a @Bean method that "
            "returns SecurityFilterChain.\n"
            "3. If configure(AuthenticationManagerBuilder) exists, extract a "
            "@Bean UserDetailsService or AuthenticationManager.\n"
            "4. If configure(WebSecurity) exists, use "
            "WebSecurityCustomizer @Bean instead.\n"
            "5. In Spring Security 6, configure logout with `new AntPathRequestMatcher(\"/logout\", \"GET\")` "
            "so GET-based logout links from templates trigger session logout cleanly.\n"
            "6. Add any necessary new imports.\n"
            "Keep all security rules (URL patterns, CSRF, etc.) identical."
        ),
        commit_summary="remove WebSecurityConfigurerAdapter (Spring Security 6)",
    ),
    MigrationRule(
        id="mvc-configurer-adapter-removal",
        description="WebMvcConfigurerAdapter removal (already deprecated in 5.x)",
        predicate=lambda m:(
            any("WebMvcConfigurerAdapter" in i for i in m.imports)
            or any(
                "WebMvcConfigurerAdapter" in ext
                for c in m.classes for ext in c.extends
            )
        ),
        migration_instruction=(
            "WebMvcConfigurerAdapter was removed in Spring 6. Change the class "
            "to implement WebMvcConfigurer directly instead of extending the "
            "adapter. Remove the WebMvcConfigurerAdapter import. Keep everything "
            "else identical."
        ),
        commit_summary="WebMvcConfigurerAdapter -> implements WebMvcConfigurer"
    ),
    MigrationRule(
        id="pbkdf2-password-encoder-upgrade",
        description="Pbkdf2PasswordEncoder constructor upgrade (Spring Security 6)",
        predicate=lambda m:any("Pbkdf2PasswordEncoder" in i for i in m.imports),
        migration_instruction=(
            "In Spring Security 6, the no-argument constructor `new Pbkdf2PasswordEncoder()` "
            "was removed. Replace `new Pbkdf2PasswordEncoder()` with "
            "`Pbkdf2PasswordEncoder.defaultsForSpringSecurity_v5_5()`. "
            "Do not change anything else in the class."
        ),
        commit_summary="Pbkdf2PasswordEncoder -> defaultsForSpringSecurity_v5_5() (Spring Security 6)"
    ),
    MigrationRule(
        id="local-server-port-package-move",
        description="LocalServerPort moved to org.springframework.boot.test.web.server in Spring Boot 3",
        predicate=lambda m:any(i == "org.springframework.boot.web.server.LocalServerPort" for i in m.imports),
        migration_instruction=(
            "In Spring Boot 3, @LocalServerPort was moved from "
            "org.springframework.boot.web.server.LocalServerPort to "
            "org.springframework.boot.test.web.server.LocalServerPort. "
            "Update the import statement to org.springframework.boot.test.web.server.LocalServerPort. "
            "Do not change anything else."
        ),
        commit_summary="LocalServerPort -> org.springframework.boot.test.web.server.LocalServerPort"
    )
]