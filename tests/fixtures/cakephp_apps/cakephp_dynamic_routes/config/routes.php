<?php
$path = '/dynamic';
$routes->connect($path, ['controller' => 'Dynamic', 'action' => 'view']);
$routes->fallbacks();
