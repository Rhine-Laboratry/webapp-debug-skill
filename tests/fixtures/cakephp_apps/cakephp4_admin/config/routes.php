<?php
$routes->prefix('Admin', function ($builder) {
    $builder->connect('/users', ['controller' => 'Users', 'action' => 'index']);
    $builder->connect('/users/delete/*', ['controller' => 'Users', 'action' => 'delete']);
});
