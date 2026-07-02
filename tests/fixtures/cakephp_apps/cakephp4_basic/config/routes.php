<?php
$routes->scope('/', function ($builder) {
    $builder->connect('/users', ['controller' => 'Users', 'action' => 'index']);
    $builder->connect('/users/add', ['controller' => 'Users', 'action' => 'add']);
    $builder->fallbacks();
});
