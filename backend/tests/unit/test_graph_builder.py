from app.ingestion.graph_builder import DependencyGraphBuilder
from app.models.manifest import JavaClassManifest, JavaFileManifest


def test_graph_builder_orders_leaves_first_and_computes_blast_radius():
    entity = JavaFileManifest(
        file_path="src/main/java/com/ex/User.java",
        package_name="com.ex",
        imports=["javax.persistence.Entity"],
        javax_imports=["javax.persistence.Entity"],
        jakarta_migratable_imports=["javax.persistence.Entity"],
        classes=[JavaClassManifest(name="User")],
    )
    service = JavaFileManifest(
        file_path="src/main/java/com/ex/UserService.java",
        package_name="com.ex",
        imports=["com.ex.User", "javax.sql.DataSource"],
        javax_imports=["javax.sql.DataSource"],
        jakarta_migratable_imports=[],
        classes=[JavaClassManifest(name="UserService")],
    )
    controller = JavaFileManifest(
        file_path="src/main/java/com/ex/UserController.java",
        package_name="com.ex",
        imports=["com.ex.UserService", "javax.validation.Valid"],
        javax_imports=["javax.validation.Valid"],
        jakarta_migratable_imports=["javax.validation.Valid"],
        classes=[JavaClassManifest(name="UserController")],
    )

    graph = DependencyGraphBuilder().build_graph([controller, service, entity])

    assert graph.total_nodes == 3
    assert graph.total_edges == 2

    depths = {t.identifier: t.migration_depth for t in graph.migration_order}
    assert depths["com.ex.User"] == 0
    assert depths["com.ex.UserService"] == 1
    assert depths["com.ex.UserController"] == 2

    # UserService only imports javax.sql.DataSource, so has_javax (Jakarta-migratable) must be False
    by_id = {t.identifier: t for t in graph.migration_order}
    assert by_id["com.ex.User"].has_javax is True
    assert by_id["com.ex.UserService"].has_javax is False

    # User affects UserService and UserController (2 ancestors)
    assert graph.high_blast_radius[0].identifier == "com.ex.User"
    assert graph.high_blast_radius[0].affected_count == 2
