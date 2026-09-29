# Legacy Upgrade Demo

This repository is a minimal Java sample that intentionally reflects an older stack:

- JDK 8
- Spring 1.2.x
- Hibernate 4.3.x
- H2 in-memory database

It is designed as a realistic starting point for a modernization or upgrade demo. The project demonstrates old-school XML configuration, direct Hibernate session management, and the kinds of issues you typically face when moving to modern Spring and Hibernate versions.

## Structure

- `src/main/java/com/example/legacy` — legacy application and service code
- `src/main/resources/applicationContext.xml` — Spring XML configuration
- `src/main/resources/hibernate.cfg.xml` — direct Hibernate config

## Run locally

```bash
mvn clean test
mvn exec:java -Dexec.mainClass=com.example.legacy.LegacyApplication
```

## Upgrade agenda

This project is useful for demonstrating:

1. Spring XML to Java config migration
2. Hibernate 4 to Hibernate 6 changes
3. JPA annotation upgrades
4. Removal of legacy transaction patterns
5. DataSource and connection management modernization
