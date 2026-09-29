package com.example.legacy.config;

import com.example.legacy.model.Customer;
import org.hibernate.SessionFactory;
import org.hibernate.cfg.Configuration;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.beans.factory.FactoryBean;
import org.springframework.beans.factory.InitializingBean;

import javax.sql.DataSource;

public class HibernateSessionFactoryBean implements FactoryBean, InitializingBean, DisposableBean {

    private DataSource dataSource;
    private SessionFactory sessionFactory;

    public void setDataSource(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    @Override
    public void afterPropertiesSet() throws Exception {
        Configuration configuration = new Configuration();
        configuration.setProperty("hibernate.dialect", "org.hibernate.dialect.H2Dialect");
        configuration.setProperty("hibernate.connection.driver_class", "org.h2.Driver");
        configuration.setProperty("hibernate.connection.url", "jdbc:h2:mem:legacy_demo;DB_CLOSE_DELAY=-1;MODE=LEGACY");
        configuration.setProperty("hibernate.connection.username", "sa");
        configuration.setProperty("hibernate.connection.password", "");
        configuration.setProperty("hibernate.hbm2ddl.auto", "create-drop");
        configuration.setProperty("hibernate.show_sql", "true");
        configuration.setProperty("hibernate.current_session_context_class", "thread");
        configuration.addAnnotatedClass(Customer.class);
        sessionFactory = configuration.buildSessionFactory();
    }

    @Override
    public Object getObject() throws Exception {
        return sessionFactory;
    }

    @Override
    public Class getObjectType() {
        return SessionFactory.class;
    }

    @Override
    public boolean isSingleton() {
        return true;
    }

    @Override
    public void destroy() throws Exception {
        if (sessionFactory != null && !sessionFactory.isClosed()) {
            sessionFactory.close();
        }
    }
}
